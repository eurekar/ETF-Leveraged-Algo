# region imports
from collections import deque
from datetime import timedelta
from starter_core import Ewma
# endregion
"""
Stage 3 trading rules ("Leveraged Trading", ch. 8). Pure Python, no LEAN imports.

Every rule is a small streaming object fed one daily close at a time:

    rule.update(close)      # once per bar
    rule.ready              # enough history?
    rule.raw()              # raw value (sign = direction, size = strength)
    rule.vote()             # +1 / -1 / 0, what Stage 3 uses

Stage 3 only uses the sign (vote). The raw values are kept so Stage 5
(forecast scaling, caps, FDM) can reuse the same objects.

Rules
-----
MovingAverageCrossover(fast, slow)
    raw = fast MA - slow MA. Simple MAs by default (same as Stage 2), or EWMAs.
    Variants used: 8/32, 16/64, 32/128, 64/256.

Breakout(n)
    Where the close sits in its n-day high-low range of closes:
        raw_t = (close - (hi + lo) / 2) / (hi - lo)      in [-0.5, +0.5]
    smoothed with an EWMA of span n/4 (as Carver does, to cut turnover).
    Variants used: 40, 80, 160, 320.

Carry (ETF version)
    An ETF has no futures roll, so carry is what you earn for holding it
    minus what it costs to finance it:
        carry = trailing 12-month distribution yield - funding rate
    Distribution yield = sum over the last 365 days of (distribution / price
    on the day before ex-date). Funding rate = risk-free rate + spread.
    raw = carry / instrument risk (annual), so it is in Sharpe-like units.
    Positive carry votes long, negative votes short.
"""


class MovingAverageCrossover:
    def __init__(self, fast, slow, ma_type="sma"):
        self.fast, self.slow = fast, slow
        self.ma_type = ma_type.lower()
        self.closes = deque(maxlen=slow)
        self.ewma_fast, self.ewma_slow = Ewma(fast), Ewma(slow)
        self.n = 0
        self.name = f"mac{fast}_{slow}"

    def update(self, close):
        self.closes.append(close)
        self.ewma_fast.update(close)
        self.ewma_slow.update(close)
        self.n += 1

    @property
    def ready(self):
        return self.n >= self.slow

    def raw(self):
        if not self.ready:
            return None
        if self.ma_type == "ewma":
            return self.ewma_fast.value - self.ewma_slow.value
        c = list(self.closes)
        return sum(c[-self.fast:]) / self.fast - sum(c) / self.slow

    def vote(self):
        return _sign(self.raw())


class Breakout:
    def __init__(self, n):
        self.n_window = n
        self.closes = deque(maxlen=n)
        self.smooth = Ewma(max(1, n // 4))
        self.value = None
        self.n = 0
        self.name = f"breakout{n}"

    def update(self, close):
        self.closes.append(close)
        self.n += 1
        if self.n < self.n_window:
            return
        hi, lo = max(self.closes), min(self.closes)
        x = 0.0 if hi == lo else (close - (hi + lo) / 2.0) / (hi - lo)
        self.value = self.smooth.update(x)

    @property
    def ready(self):
        return self.value is not None

    def raw(self):
        return self.value

    def vote(self):
        return _sign(self.raw())


class Carry:
    """ETF carry: trailing 12-month distribution yield minus funding cost."""

    LOOKBACK = timedelta(days=365)

    def __init__(self, funding_spread=0.0):
        self.funding_spread = funding_spread
        self.payments = deque()      # (ex_date, yield of that payment)
        self.first_date = None
        self.last_date = None
        self.funding_rate = None     # annual, decimal, e.g. 0.05
        self.risk = None             # annual instrument risk, decimal
        self.name = "carry"

    def add_distribution(self, date, amount, ref_price):
        """One dividend / coupon payment. ref_price = price before the ex-date."""
        if ref_price and ref_price > 0 and amount:
            self.payments.append((date, float(amount) / float(ref_price)))

    def update(self, date, risk_free_rate, instrument_risk=None):
        if self.first_date is None:
            self.first_date = date
        self.last_date = date
        self.funding_rate = risk_free_rate
        self.risk = instrument_risk
        while self.payments and self.payments[0][0] <= date - self.LOOKBACK:
            self.payments.popleft()

    @property
    def ready(self):
        # Needs a full year of observed distributions and a funding rate.
        return (self.first_date is not None and self.funding_rate is not None
                and self.last_date - self.first_date >= self.LOOKBACK)

    def distribution_yield(self):
        return sum(y for d, y in self.payments if d <= self.last_date)

    def carry(self):
        if not self.ready:
            return None
        return self.distribution_yield() - (self.funding_rate + self.funding_spread)

    def raw(self):
        c = self.carry()
        if c is None:
            return None
        return c / self.risk if self.risk else c

    def vote(self):
        return _sign(self.carry())


def _sign(x):
    if x is None:
        return 0
    return 1 if x > 0 else -1 if x < 0 else 0


# ---------------------------------------------------------------------------
# Rule set parsing
# ---------------------------------------------------------------------------
DEFAULT_RULES = "mac8,mac16,mac32,mac64,breakout40,breakout80,breakout160,breakout320,carry"


def build_rules(spec=DEFAULT_RULES, ma_type="sma", funding_spread=0.0):
    """
    Build rule objects from a comma list.
      macF        crossover F / 4F  (mac16 = 16/64)
      macF_S      crossover F / S
      breakoutN   breakout over N days
      carry       ETF carry
    Returns {name: (family, rule)}; family is "mac", "breakout" or "carry".
    """
    out = {}
    for tok in [t.strip().lower() for t in spec.split(",") if t.strip()]:
        if tok.startswith("mac"):
            parts = tok[3:].split("_")
            fast = int(parts[0])
            slow = int(parts[1]) if len(parts) > 1 else 4 * fast
            r = MovingAverageCrossover(fast, slow, ma_type)
            out[r.name] = ("mac", r)
        elif tok.startswith("breakout"):
            r = Breakout(int(tok[len("breakout"):]))
            out[r.name] = ("breakout", r)
        elif tok == "carry":
            out["carry"] = ("carry", Carry(funding_spread))
        else:
            raise ValueError(f"Unknown rule '{tok}'")
    return out
