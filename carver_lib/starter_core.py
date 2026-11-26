# region imports
from collections import deque
from dataclasses import dataclass
import math
# endregion
"""
Pure-Python core of Robert Carver's Starter System ("Leveraged Trading", ch. 5-6).

No QuantConnect imports here, so the same logic can be unit-tested offline
and reused by main.py inside LEAN.

Rules implemented
-----------------
Opening rule : trend signal from a 16/64 moving-average crossover
               (simple MAs as in the book, or EWMAs as an option).
Instrument risk : annualised std-dev of daily % returns over the last 25 days
               (daily std x 16, since sqrt(256) = 16), same as
               scheduler/update_intrument_risks.py in StarterAlgo.
Position size : notional = capital x target_risk x weight x IDM / instrument_risk
Closing rule : trailing stop. Stop gap (in price units) is fixed at entry:
               stop_gap = X x instrument_risk x entry_price, X = 0.5.
               It trails the high-water mark (longs) / low-water mark (shorts)
               and is checked against the daily close.
Re-entry     : after a stop-out, do not re-open in the same direction as the
               trade just closed; wait until the signal points the other way
               (same as the overtrading rule in signal_detector.py).
Start        : with wait_for_flip=True the start of trading is treated like a
               stop-out: the first trade waits for the first signal flip.
"""

FAST = 16
SLOW = 64
RISK_WINDOW = 25
TRADING_DAYS_SQRT = 16.0  # sqrt(256)


def sma(values, n):
    if len(values) < n:
        return None
    return sum(list(values)[-n:]) / n


class Ewma:
    """Exponentially weighted moving average with span n (alpha = 2 / (n + 1))."""

    def __init__(self, span):
        self.alpha = 2.0 / (span + 1.0)
        self.value = None

    def update(self, x):
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        return self.value


@dataclass
class OpenTrade:
    direction: int          # +1 long, -1 short
    entry_price: float
    stop_gap: float         # price units, fixed at entry
    water_mark: float       # highest close since entry (long) / lowest (short)

    @property
    def stop_price(self):
        return self.water_mark - self.stop_gap if self.direction > 0 else self.water_mark + self.stop_gap

    def update(self, close):
        """Move the water mark with the close. Returns True if the stop is hit."""
        if self.direction > 0:
            self.water_mark = max(self.water_mark, close)
            return close <= self.stop_price
        self.water_mark = min(self.water_mark, close)
        return close >= self.stop_price


class StarterSystem:
    """State for one instrument. Feed it one daily close at a time."""

    def __init__(self, signal_type="sma", fast=FAST, slow=SLOW,
                 risk_window=RISK_WINDOW, stop_fraction=0.5, allow_short=True,
                 wait_for_flip=False):
        self.signal_type = signal_type.lower()
        self.fast, self.slow = fast, slow
        self.risk_window = risk_window
        self.stop_fraction = stop_fraction
        self.allow_short = allow_short
        # wait_for_flip: treat the start like a stop-out. The first time we are asked
        # to open, block the current signal direction and wait for it to flip, so the
        # first trade opens on a fresh crossover rather than mid-trend.
        self.wait_for_flip = wait_for_flip
        self._started = False
        self.closes = deque(maxlen=max(slow, risk_window + 1))
        self.ewma_fast, self.ewma_slow = Ewma(fast), Ewma(slow)
        self.n = 0
        self.trade = None            # OpenTrade or None
        self.last_closed_dir = 0     # direction of the last closed trade

    # ---------- indicators ----------
    def add_close(self, close):
        self.closes.append(close)
        self.ewma_fast.update(close)
        self.ewma_slow.update(close)
        self.n += 1

    @property
    def ready(self):
        return self.n >= max(self.slow, self.risk_window + 1)

    def signal(self):
        """+1 if fast MA > slow MA, -1 if below, 0 if not ready / equal."""
        if not self.ready:
            return 0
        if self.signal_type == "ewma":
            f, s = self.ewma_fast.value, self.ewma_slow.value
        else:
            f, s = sma(self.closes, self.fast), sma(self.closes, self.slow)
        if f > s:
            return 1
        if f < s:
            return -1
        return 0

    def instrument_risk(self):
        """Annualised volatility of daily % returns over the last risk_window days."""
        px = list(self.closes)[-(self.risk_window + 1):]
        if len(px) < self.risk_window + 1:
            return None
        rets = [px[i] / px[i - 1] - 1.0 for i in range(1, len(px))]
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)   # sample std, like pandas
        return math.sqrt(var) * TRADING_DAYS_SQRT

    # ---------- decisions ----------
    def wants_to_open(self):
        """Direction to open now (+1/-1) or 0. Only meaningful when flat."""
        if self.trade is not None:
            return 0
        sig = self.signal()
        if sig == 0:
            return 0
        if not self._started:
            self._started = True
            if self.wait_for_flip:
                self.last_closed_dir = sig   # as if a trade in this direction just stopped out
                return 0
        if sig == self.last_closed_dir:
            return 0   # overtrading rule: wait for the signal to flip first
        self.last_closed_dir = 0   # signal has flipped, block lifted
        if sig < 0 and not self.allow_short:
            return 0
        return sig

    def open(self, direction, price, risk):
        gap = self.stop_fraction * risk * price
        self.trade = OpenTrade(direction, price, gap, price)
        return self.trade

    def check_stop(self, close):
        """Update the trailing stop with today's close. Returns True if we must close."""
        return self.trade is not None and self.trade.update(close)

    def close(self):
        if self.trade is not None:
            self.last_closed_dir = self.trade.direction
        self.trade = None


def position_size(capital, target_risk, instrument_risk, price, weight=1.0, idm=1.0):
    """Shares to hold: notional = capital x target_risk x weight x IDM / instrument_risk."""
    if not instrument_risk or instrument_risk <= 0 or price <= 0:
        return 0
    notional = capital * target_risk * weight * idm / instrument_risk
    return int(notional / price)
