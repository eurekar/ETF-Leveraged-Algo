"""Offline checks for Stage 4 (continuous trading with a buffer). Run: python -m pytest tests"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "carver_lib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from buffer import buffered_trade, buffer_edges, average_position, ContinuousSystem  # noqa: E402
from test_stage3 import PX, DATES, synthetic_prices                                  # noqa: E402


def test_average_position():
    # 100k x 12% x 0.1 x 2 / 15% / $50 = 320 shares
    assert abs(average_position(100_000, 0.12, 0.15, 50.0, 0.1, 2.0) - 320) < 1e-9
    assert average_position(100_000, 0.12, 0, 50.0) == 0
    assert average_position(100_000, 0.12, None, 50.0) == 0


def test_buffer_rules():
    avg = 1000.0
    assert buffer_edges(1000, avg, 0.1) == (900, 1100)
    # inside the buffer: no trade
    assert buffered_trade(950, 1000, avg, 0.1, 1) == (0, "")
    assert buffered_trade(1100, 1000, avg, 0.1, 1) == (0, "")
    # below the lower edge: buy up to the edge, not to the target
    assert buffered_trade(800, 1000, avg, 0.1, 1) == (100, "adjust")
    # above the upper edge: sell down to the edge
    assert buffered_trade(1300, 1000, avg, 0.1, 1) == (-200, "adjust")
    # flat and a direction: straight to the target
    assert buffered_trade(0, 1000, avg, 0.1, 1) == (1000, "open")
    # flip: close and reverse in one order, straight to the target
    assert buffered_trade(1000, -950, 950, 0.1, -1) == (-1950, "flip")
    # tie (direction 0): buffer around zero, trim to 10% of the average position
    assert buffered_trade(1000, 0, avg, 0.1, 0) == (-900, "adjust")
    assert buffered_trade(50, 0, avg, 0.1, 0) == (0, "")
    assert buffered_trade(0, 0, avg, 0.1, 0) == (0, "")
    # edges are rounded towards the target
    assert buffered_trade(0, 10.4, 10.4, 0.1, 1) == (10, "open")
    assert buffered_trade(8, 10.4, 10.4, 0.1, 1) == (2, "adjust")      # lower edge 9.36 -> 10


def simulate(system, px=PX, equity=100_000.0, target_risk=0.12, weight=0.1, idm=2.0):
    """Same daily loop as stage4_continuous/main.py for one ETF, fixed equity, fills at the close.
    Returns list of (i, direction, position_after, reason) for every trade, and daily positions."""
    pos, trades, path = 0, [], []
    for i, c in enumerate(px):
        system.add_close(float(c), DATES[i], 0.02)
        if not system.ready:
            path.append(None)
            continue
        d, target, avg = system.target(equity, target_risk, float(c), weight, idm)
        qty, reason = system.order_for(pos, d, target, avg)
        if qty:
            pos += qty
            trades.append((i, d, pos, reason))
        path.append((d, pos, target, avg))
    return trades, path


def test_position_always_matches_direction_and_buffer():
    for seed in range(3):
        s = ContinuousSystem(rules_spec="mac16")
        trades, path = simulate(s, synthetic_prices(seed))
        assert trades
        for p in path:
            if p is None:
                continue
            d, pos, target, avg = p
            if d != 0:
                assert (pos > 0) - (pos < 0) == d                  # always on the signal's side
            lo, hi = buffer_edges(target, avg, s.buffer_fraction)
            assert lo - 1 <= pos <= hi + 1                         # within the buffer (+/- rounding)


def test_flip_mode_wide_buffer_changes_only_on_flips():
    """Regression check: size only on a direction change, never adjust -> one trade per flip."""
    for spec in ("mac16", None):
        kw = {"rules_spec": spec} if spec else {}
        s = ContinuousSystem(resize="flip", buffer_fraction=100, **kw)
        trades, path = simulate(s)
        dirs = [p[0] for p in path if p is not None and p[0] != 0]   # ties hold the position
        changes = sum(1 for a, b in zip(dirs, dirs[1:]) if b != a)
        assert all(r in ("open", "flip") for _, _, _, r in trades)
        assert dirs and len(trades) == 1 + changes
        assert [d for _, d, _, _ in trades] == [dirs[0]] + [b for a, b in zip(dirs, dirs[1:]) if b != a]


def test_daily_resize_trades_more_than_flip_mode():
    daily, _ = simulate(ContinuousSystem(rules_spec="mac16"))
    flip, _ = simulate(ContinuousSystem(rules_spec="mac16", resize="flip", buffer_fraction=100))
    assert len(daily) > len(flip)
    assert {r for *_, r in daily} >= {"adjust", "flip"}
    # A wider buffer means fewer adjustments
    wide, _ = simulate(ContinuousSystem(rules_spec="mac16", buffer_fraction=0.2))
    narrow, _ = simulate(ContinuousSystem(rules_spec="mac16", buffer_fraction=0.05))
    assert len(narrow) > len(daily) > len(wide)
    print(f"  synthetic path trades: buffer 5% {len(narrow)}, 10% {len(daily)}, 20% {len(wide)}, "
          f"flip-only {len(flip)}")


def test_no_shorts():
    s = ContinuousSystem(rules_spec="mac16", allow_short=False)
    _, path = simulate(s)
    assert all(p[1] >= 0 for p in path if p is not None)


def test_wait_for_flip():
    s = ContinuousSystem(rules_spec="mac16", wait_for_flip=True)
    plain = ContinuousSystem(rules_spec="mac16")
    t_wait, path = simulate(s)
    t_plain, _ = simulate(plain)
    first_ready = next(i for i, p in enumerate(path) if p is not None)
    assert t_plain[0][0] == first_ready                      # plain: trades on the first ready day
    assert t_wait[0][0] > first_ready and t_wait[0][3] == "open"


def test_full_rules_run():
    s = ContinuousSystem()
    trades, _ = simulate(s)
    assert trades and trades[0][3] == "open"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
