"""
Check the risk-free rate a Stage 3 backtest used against an outside series (FRED DTB3).

LEAN's default risk-free model is the Fed primary credit rate (FRED PCREDIT8). Stage 3
charts it as "Risk-free rate (%)" / "LEAN risk-free" (older runs: "Funding rate" /
"risk-free", in decimals). This script reads that series from a backtest result JSON,
lists the dates it changed, and compares it year by year with a FRED CSV.

Get the outside series: https://fred.stlouisfed.org/series/DTB3 -> Download -> CSV.
DTB3 is the 3-month T-bill secondary market rate, percent, discount basis, daily.
Any FRED daily or step series in percent works (e.g. PCREDIT8 itself, DFF, DGS3MO).

Usage:
  python research/check_risk_free.py path/to/backtest.json [--fred DTB3.csv] [--csv out.csv]
      [--series "Risk-free used"]

--series picks which line of the "Risk-free rate (%)" chart to read: "LEAN risk-free"
(default, primary credit), "Risk-free used" (the rate the run used: DTB3 with rf_source=dtb3)
or "Carry funding (rf + spread)". With rf_source=dtb3, "Risk-free used" vs DTB3 should show a
gap near zero (the weekly sampling and the one-day publication lag leave a few bp).

Needs pandas only.
"""
import argparse
import json

import pandas as pd


def load_rf(result, series="LEAN risk-free"):
    """Daily series of one line of the backtest's risk-free chart, in percent."""
    charts = result.get("charts") or result.get("Charts") or {}
    for chart_name, series_name, scale in (("Risk-free rate (%)", series, 1.0),
                                           ("Funding rate", "risk-free", 100.0)):
        chart = charts.get(chart_name) or {}
        series = chart.get("series") or chart.get("Series") or {}
        s = series.get(series_name)
        if not s:
            continue
        pts = []
        for p in s.get("values") or s.get("Values") or []:
            t, v = (p["x"], p["y"]) if isinstance(p, dict) else (p[0], p[-1])
            if v is not None:
                pts.append((t, float(v) * scale))
        if pts:
            rf = pd.Series([v for _, v in pts], index=pd.to_datetime([t for t, _ in pts], unit="s"))
            rf = rf[~rf.index.duplicated(keep="last")].sort_index()
            return rf.resample("1D").last().ffill()
    raise ValueError(f"No '{series}' series found (looked for 'Risk-free rate (%)' and 'Funding rate' charts)")


def load_fred(path):
    df = pd.read_csv(path)
    date_col = df.columns[0]           # "DATE" or "observation_date"
    val_col = df.columns[1]
    s = pd.to_numeric(df[val_col], errors="coerce")   # FRED writes "." for missing days
    s.index = pd.to_datetime(df[date_col])
    return s.dropna().rename(val_col)


def change_dates(rf):
    chg = rf[rf.diff().fillna(1) != 0]
    return chg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("backtest")
    ap.add_argument("--fred", help="FRED CSV to compare with, e.g. DTB3.csv")
    ap.add_argument("--csv", help="write the yearly comparison here")
    ap.add_argument("--series", default="LEAN risk-free", help="line of the 'Risk-free rate (%%)' chart")
    a = ap.parse_args()

    with open(a.backtest, encoding="utf-8") as f:
        rf = load_rf(json.load(f), a.series)

    print("Rate changes seen in the backtest (chart is sampled weekly and on change days,")
    print("so a change can show up to a few days after the Fed's effective date):\n")
    print("| first seen | rate % |\n|---|---|")
    for d, v in change_dates(rf).items():
        print(f"| {d:%Y-%m-%d} | {v:.2f} |")

    if not a.fred:
        return
    ext = load_fred(a.fred)
    name = ext.name
    both = pd.concat([rf.rename("lean"), ext.rename(name)], axis=1, sort=True).dropna()
    yearly = both.groupby(both.index.year).mean()
    yearly["gap (lean - %s)" % name] = yearly["lean"] - yearly[name]
    print(f"\nYearly averages, percent (business days where both exist):\n")
    print(f"| year | backtest ({a.series}) | {name} | gap |\n|---|---|---|---|")
    for y, r in yearly.iterrows():
        print(f"| {y} | {r['lean']:.2f} | {r[name]:.2f} | {r.iloc[2]:+.2f} |")
    gap = both["lean"] - both[name]
    print(f"\nWhole sample: mean gap {gap.mean():+.2f} pp, min {gap.min():+.2f}, max {gap.max():+.2f}")
    if a.csv:
        yearly.to_csv(a.csv)
        print(f"Wrote {a.csv}")


if __name__ == "__main__":
    main()
