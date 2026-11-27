"""
Compare QuantConnect backtests of different stages on one scorecard.

Input: backtest result JSON files, either
  - from the LEAN CLI:  backtests/<timestamp>/<backtest-id>.json  (lean backtest / lean cloud backtest --download)
  - from the QC web IDE: Backtest results -> Download results (JSON)

Usage:
  python research/compare_stages.py stage2=path/to/stage2.json stage3=path/to/stage3.json \
      [--split 2016-01-01] [--csv out.csv]

Prints a Markdown table with: annual return, realised vol, Sharpe, max drawdown,
longest drawdown, monthly skew, orders (opens, stops, buffer adjusts), fees, turnover, and the same metrics for
the two halves of the sample (split date). Needs numpy and pandas only.
"""
import argparse
import json
import math
import sys

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _points(values):
    """QC chart points come as {'x': t, 'y': v} or [t, open, high, low, close] or [t, v]."""
    for p in values:
        if isinstance(p, dict):
            yield p["x"], p["y"]
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            yield p[0], p[-1]


def load_equity(result):
    charts = result.get("charts") or result.get("Charts") or {}
    chart = charts.get("Strategy Equity") or {}
    series = chart.get("series") or chart.get("Series") or {}
    eq = series.get("Equity") or {}
    values = eq.get("values") or eq.get("Values") or []
    pts = [(t, v) for t, v in _points(values) if v is not None]
    if not pts:
        raise ValueError("No 'Strategy Equity' / 'Equity' series in this result file")
    s = pd.Series([v for _, v in pts], index=pd.to_datetime([t for t, _ in pts], unit="s"))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.resample("1D").last().dropna()


def load_orders(result):
    orders = result.get("orders") or result.get("Orders") or {}
    rows = orders.values() if isinstance(orders, dict) else orders
    out = []
    for o in rows:
        status = o.get("status", o.get("Status"))
        if status not in (3, "Filled", "filled"):
            continue
        qty = float(o.get("quantity", o.get("Quantity", 0)))
        price = float(o.get("price", o.get("Price", 0)))
        t = o.get("lastFillTime") or o.get("time") or o.get("Time")
        out.append({"time": pd.to_datetime(t, utc=True).tz_localize(None) if t else pd.NaT,
                    "notional": abs(qty * price), "tag": o.get("tag", o.get("Tag", ""))})
    return pd.DataFrame(out, columns=["time", "notional", "tag"])


def load_stat(result, key):
    stats = result.get("statistics") or result.get("Statistics") or {}
    v = stats.get(key)
    if v is None:
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("%", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def scorecard(equity, orders=None):
    rets = equity.pct_change().dropna()
    rets = rets[rets.index.dayofweek < 5]
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    vol = rets.std() * 16
    sharpe = rets.mean() / rets.std() * 16 if rets.std() > 0 else float("nan")
    peak = equity.cummax()
    dd = equity / peak - 1
    underwater = dd < 0
    longest, start = 0, None
    for t, u in underwater.items():
        if u:
            start = start or t
            longest = max(longest, (t - start).days)
        else:
            start = None
    monthly = equity.resample("ME").last().pct_change().dropna()
    row = {
        "Annual return %": 100 * cagr,
        "Ann. vol %": 100 * vol,
        "Sharpe": sharpe,
        "Max DD %": 100 * dd.min(),
        "Longest DD (days)": longest,
        "Monthly skew": monthly.skew() if len(monthly) > 2 else float("nan"),
    }
    if orders is not None and len(orders):
        o = orders.dropna(subset=["time"])
        o = o[(o.time >= equity.index[0]) & (o.time <= equity.index[-1] + pd.Timedelta(days=1))]
        row["Orders"] = len(o)
        row["Opens"] = int(o.tag.str.startswith("Open").sum())
        row["Stops"] = int(o.tag.str.startswith("Stop").sum())
        row["Adjusts"] = int(o.tag.str.startswith("Adjust").sum())   # Stage 4+ buffer trades
        row["Turnover (x equity/yr)"] = o.notional.sum() / equity.mean() / years
    return row


def compare(named_results, split=None):
    rows = {}
    for name, res in named_results.items():
        eq, orders = load_equity(res), load_orders(res)
        row = scorecard(eq, orders)
        fees = load_stat(res, "Total Fees")
        if fees is not None:
            years = (eq.index[-1] - eq.index[0]).days / 365.25
            row["Fees % equity/yr"] = 100 * abs(fees) / eq.mean() / years
        rows[name] = row
        if split is not None:
            cut = pd.Timestamp(split)
            for label, part in (("<", eq[eq.index < cut]), (">=", eq[eq.index >= cut])):
                if len(part) > 30:
                    rows[f"{name} {label}{cut.year}"] = scorecard(part, orders)
    return pd.DataFrame(rows)


def to_markdown(df):
    cols = [""] + [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for idx, row in df.iterrows():
        lines.append("| " + " | ".join([str(idx)] + [str(v) for v in row]) + " |")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="name=path.json pairs, in stage order")
    ap.add_argument("--split", default="2016-01-01", help="date to split the sample in two ('' for none)")
    ap.add_argument("--csv", help="also write the table to this CSV file")
    a = ap.parse_args(argv)
    named = {}
    for r in a.runs:
        name, _, path = r.partition("=")
        if not path:
            name, path = r.rsplit("/", 1)[-1].split(".")[0], r
        with open(path) as f:
            named[name] = json.load(f)
    table = compare(named, a.split or None)
    fmt = table.copy().astype(object)
    for idx in fmt.index:
        for col in fmt.columns:
            v = table.loc[idx, col]
            fmt.loc[idx, col] = "" if v is None or (isinstance(v, float) and math.isnan(v)) else (
                f"{v:,.0f}" if idx in ("Orders", "Opens", "Stops", "Adjusts", "Longest DD (days)") else f"{v:,.2f}")
    print(to_markdown(fmt))
    if a.csv:
        table.to_csv(a.csv)


if __name__ == "__main__":
    sys.exit(main())
