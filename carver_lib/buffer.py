# region imports
import math
from combine import MultiRuleSystem
from rules import DEFAULT_RULES
# endregion
"""
Stage 4: continuous trading ("Leveraged Trading", the chapter after the
multiple-rules chapter: drop stop-losses, re-size every day, buffer the trades).

Every day, for each instrument:

    direction       = sign of the Stage 3 combined vote (+1, -1, or 0 on a tie)
    average_pos     = equity x target_risk x weight x IDM / instrument_risk / price   (shares)
    target          = direction x average_pos
    buffer          = buffer_fraction x average_pos          (10% in the book)

    if the current position has the opposite sign to the direction (a flip),
    or is flat while the direction is not zero (first entry):
        trade straight to the target          -> close and reverse in one order
    elif current < target - buffer:  buy  up to   target - buffer   (nearest edge)
    elif current > target + buffer:  sell down to target + buffer   (nearest edge)
    else:                            no trade

There are no stops and no re-entry rule: the position is always the one the
signal implies, so a position only closes when the signal flips (or shrinks to
the buffer edge on a tie).

Direction stays binary, as in Stage 3. Forecast scaling and the FDM wait for
Stage 5 (non-binary forecasts).

Nothing here imports LEAN, so it is tested offline (tests/test_stage4.py).
"""

DEFAULT_BUFFER = 0.10


def average_position(equity, target_risk, instrument_risk, price, weight=1.0, idm=1.0):
    """Shares of the standard (average) position, unrounded. 0 if inputs are not usable."""
    if not instrument_risk or instrument_risk <= 0 or not price or price <= 0:
        return 0.0
    return equity * target_risk * weight * idm / instrument_risk / price


def buffer_edges(target, avg_position, buffer_fraction):
    """(lower, upper) shares. Width is a fraction of the average position, not of the target,
    so a zero target still has a buffer around it."""
    w = buffer_fraction * abs(avg_position)
    return target - w, target + w


def buffered_trade(current, target, avg_position, buffer_fraction, direction):
    """Shares to trade (signed int) and the reason: 'open', 'flip', 'adjust' or ''.

    current          shares held now (including orders already sent and not yet filled)
    target           target shares (float), direction x average position
    avg_position     average position in shares (float, >= 0)
    buffer_fraction  e.g. 0.10
    direction        +1 / -1 / 0
    """
    cur_sign = (current > 0) - (current < 0)
    if direction != 0 and cur_sign != direction:
        trade = int(round(target)) - int(current)
        return trade, ("flip" if cur_sign != 0 else "open") if trade != 0 else ""
    lower, upper = buffer_edges(target, avg_position, buffer_fraction)
    if current < lower:
        trade = math.ceil(lower - 1e-9) - int(current)      # round towards the target
    elif current > upper:
        trade = math.floor(upper + 1e-9) - int(current)
    else:
        return 0, ""
    return trade, ("adjust" if trade != 0 else "")


class ContinuousSystem(MultiRuleSystem):
    """Stage 3 signal, Stage 4 position management. One instance per instrument.

    resize = "daily": the target is re-sized every day with current equity and risk (Stage 4).
    resize = "flip":  the size is set when the direction changes and then held (risk and equity
                      frozen at that point). Used for the regression check: with a very wide
                      buffer the position then changes only on signal flips.
    wait_for_flip:    if True, the direction seen when the system first becomes ready is
                      ignored until the vote flips (as Stages 2 and 3). Default False: a
                      continuous system simply holds what the signal says.
    """

    def __init__(self, rules_spec=DEFAULT_RULES, family_weights=None, ma_type="sma",
                 funding_spread=0.0, risk_window=25, allow_short=True,
                 buffer_fraction=DEFAULT_BUFFER, resize="daily", wait_for_flip=False):
        super().__init__(rules_spec=rules_spec, family_weights=family_weights, ma_type=ma_type,
                         funding_spread=funding_spread, risk_window=risk_window,
                         allow_short=allow_short, wait_for_flip=False)
        if resize not in ("daily", "flip"):
            raise ValueError("resize must be 'daily' or 'flip'")
        self.buffer_fraction = float(buffer_fraction)
        self.resize = resize
        self._blocked = None if wait_for_flip else 0   # None: not decided yet; 0: no block
        self._last_dir = 0
        self._frozen_avg = None

    def direction(self):
        """+1 / -1 / 0 position direction for today."""
        sig = self.signal()
        if self._blocked is None:                      # first ready day with wait_for_flip
            if sig == 0:
                return 0
            self._blocked = sig
        if self._blocked:
            if sig == self._blocked or sig == 0:
                return 0
            self._blocked = 0                          # the vote has flipped: block lifted
        if sig < 0 and not self.allow_short:
            return 0
        return sig

    def target(self, equity, target_risk, price, weight=1.0, idm=1.0):
        """(direction, target shares, average position shares), floats, before any leverage cap."""
        d = self.direction()
        avg = average_position(equity, target_risk, self.instrument_risk(), price, weight, idm)
        if self.resize == "flip":
            if d != 0 and (d != self._last_dir or self._frozen_avg is None):
                self._frozen_avg = avg
            avg = self._frozen_avg if self._frozen_avg is not None else avg
        if d != 0:
            self._last_dir = d
        return d, d * avg, avg

    def order_for(self, current, direction, target, avg_position):
        """(shares to trade, reason). Named order_for because StarterSystem uses .trade."""
        return buffered_trade(current, target, avg_position, self.buffer_fraction, direction)
