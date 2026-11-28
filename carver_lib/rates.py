"""
Interest-rate series for the backtest, kept apart from LEAN so it can be tested offline.

Two different rates, two different jobs:
  risk-free rate  performance statistics (QC's Sharpe subtracts it). The 3-month T-bill, FRED DTB3.
  funding rate    the carry rule's financing cost: risk-free rate + funding_spread (broker markup).

StepRate holds a dated series and answers "what was the latest published value on this date",
with a publication lag so the backtest never sees a number before it was released.
"""
import bisect
from datetime import date, datetime, timedelta

FRED_DTB3_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3"


def _to_date(d):
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def parse_fred_csv(text, percent=True):
    """FRED CSV (header 'observation_date,DTB3' or 'DATE,DTB3') -> sorted [(date, annual decimal)].
    Missing days ('.' or blank) are skipped. percent=True divides by 100."""
    out = []
    for line in text.strip().splitlines()[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) < 2 or parts[1] in ("", "."):
            continue
        try:
            v = float(parts[1])
            d = _to_date(parts[0])
        except ValueError:
            continue
        out.append((d, v / 100.0 if percent else v))
    out.sort()
    return out


class StepRate:
    """Dated rate series, forward-filled. rate_at(d) = latest value observed on or before d - lag_days."""

    def __init__(self, points=(), lag_days=1, name="rate"):
        self.lag = timedelta(days=lag_days)
        self.name = name
        self.dates, self.values = [], []
        for d, v in sorted(points):
            self.add(d, v)

    def add(self, d, v):
        d = _to_date(d)
        if self.dates and d <= self.dates[-1]:
            if d == self.dates[-1]:
                self.values[-1] = float(v)
                return
            i = bisect.bisect_left(self.dates, d)   # out of order: insert
            if i < len(self.dates) and self.dates[i] == d:
                self.values[i] = float(v)
            else:
                self.dates.insert(i, d)
                self.values.insert(i, float(v))
            return
        self.dates.append(d)
        self.values.append(float(v))

    def __len__(self):
        return len(self.dates)

    @property
    def first_date(self):
        return self.dates[0] if self.dates else None

    @property
    def last_date(self):
        return self.dates[-1] if self.dates else None

    def rate_at(self, d):
        """None before the first usable observation."""
        if not self.dates:
            return None
        cutoff = _to_date(d) - self.lag
        i = bisect.bisect_right(self.dates, cutoff)
        return self.values[i - 1] if i > 0 else None

