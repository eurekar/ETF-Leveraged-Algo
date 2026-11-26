import sys, numpy as np, pandas as pd
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "carver_lib"))
from starter_core import StarterSystem, position_size, OpenTrade

rng = np.random.default_rng(0)
drift = np.repeat(rng.choice([0.0015, -0.0015, 0.0], 20), 100)
rets = drift + rng.normal(0, 0.01, len(drift))
px = pd.Series(100 * np.exp(np.cumsum(rets)))

for st in ["sma", "ewma"]:
    s = StarterSystem(signal_type=st)
    trades, risks = [], []
    for i, c in enumerate(px):
        s.add_close(c)
        if not s.ready: continue
        risks.append((i, s.instrument_risk()))
        if s.trade is not None:
            if s.check_stop(c):
                trades.append((s.trade.direction, s.trade.entry_price, c)); s.close()
            continue
        d = s.wants_to_open()
        if d: s.open(d, c, s.instrument_risk())
    pnl = sum(d * (x / e - 1) for d, e, x in trades)
    print(st, "trades", len(trades), "longs", sum(t[0] > 0 for t in trades), "sum pct pnl %.2f" % pnl)
    # re-entry rule: consecutive trades never share direction unless signal flipped in between -> just report
    same = sum(trades[i][0] == trades[i-1][0] for i in range(1, len(trades)))
    print("  consecutive same-direction trades:", same)

# instrument risk vs pandas (as in update_intrument_risks.py)
pd_risk = px.pct_change().rolling(25).std() * 16
i, r = risks[-1]
print("risk core %.6f pandas %.6f" % (r, pd_risk.iloc[i]))
assert abs(r - pd_risk.iloc[i]) < 1e-9

# SMA signal vs pandas
s = StarterSystem(); [s.add_close(c) for c in px]
exp = np.sign(px.rolling(16).mean().iloc[-1] - px.rolling(64).mean().iloc[-1])
assert s.signal() == exp

# trailing stop
t = OpenTrade(1, 450, 0.5 * 0.18 * 450, 450)
assert abs(t.stop_price - 409.5) < 1e-9
t.update(460); assert abs(t.stop_price - 419.5) < 1e-9
assert not t.update(455)
assert t.update(419)
ts = OpenTrade(-1, 100, 10, 100); ts.update(90); assert ts.stop_price == 100; assert ts.update(101)

# book example: $10k, 12%, 18%, $450 -> 14 shares (6,667 / 450 = 14.8, floor)
assert position_size(10_000, 0.12, 0.18, 450) == 14

# long-only: block lifts after signal flips even without a short trade
s = StarterSystem(allow_short=False); s.last_closed_dir = 1
s.signal = lambda: -1; assert s.wants_to_open() == 0 and s.last_closed_dir == 0
s.signal = lambda: 1; assert s.wants_to_open() == 1
print("all checks passed")
