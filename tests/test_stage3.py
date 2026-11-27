"""Offline checks for Stage 3 (rules + combined vote). Run: python tests/test_stage3.py (or pytest)."""
import itertools
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "carver_lib"))
from starter_core import StarterSystem                      # noqa: E402
from rules import MovingAverageCrossover, Breakout, Carry, build_rules, DEFAULT_RULES  # noqa: E402
from combine import MultiRuleSystem, rule_weights            # noqa: E402


def synthetic_prices(seed=0, n_regimes=20, regime_len=100):
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([0.0015, -0.0015, 0.0], n_regimes), regime_len)
    rets = drift + rng.normal(0, 0.01, len(drift))
    return pd.Series(100 * np.exp(np.cumsum(rets)))


PX = synthetic_prices()
DATES = [date(2005, 1, 3) + timedelta(days=i) for i in range(len(PX))]


def run(system, px=PX, rf=0.02, distributions=None):
    """Same daily loop as main.py. Returns list of (open_index, direction, close_index)."""
    distributions = distributions or {}
    trades, open_i = [], None
    for i, c in enumerate(px):
        if i in distributions and hasattr(system, "add_distribution"):
            system.add_distribution(DATES[i], *distributions[i])
        if isinstance(system, MultiRuleSystem):
            system.add_close(c, DATES[i], rf)
        else:
            system.add_close(c)
        if not system.ready:
            continue
        if system.trade is not None:
            if system.check_stop(c):
                trades.append((open_i, system.trade.direction, i))
                system.close()
            continue
        d = system.wants_to_open()
        if d:
            system.open(d, c, system.instrument_risk())
            open_i = i
    return trades


def test_mac_sma_matches_pandas():
    for f, s in [(8, 32), (16, 64), (32, 128), (64, 256)]:
        r = MovingAverageCrossover(f, s, "sma")
        for c in PX:
            r.update(c)
        exp = PX.rolling(f).mean().iloc[-1] - PX.rolling(s).mean().iloc[-1]
        assert abs(r.raw() - exp) < 1e-9, (f, s)
        assert r.vote() == np.sign(exp)


def test_mac_ewma_matches_pandas():
    r = MovingAverageCrossover(16, 64, "ewma")
    for c in PX:
        r.update(c)
    exp = PX.ewm(span=16, adjust=False).mean().iloc[-1] - PX.ewm(span=64, adjust=False).mean().iloc[-1]
    assert abs(r.raw() - exp) < 1e-9


def test_breakout_matches_pandas():
    for n in (40, 80, 160, 320):
        r = Breakout(n)
        for c in PX:
            r.update(c)
        hi, lo = PX.rolling(n).max(), PX.rolling(n).min()
        x = ((PX - (hi + lo) / 2) / (hi - lo)).dropna()
        exp = x.ewm(span=max(1, n // 4), adjust=False).mean().iloc[-1]
        assert abs(r.raw() - exp) < 1e-12, n
        assert -0.5 <= r.raw() <= 0.5


def test_breakout_direction():
    up = Breakout(40)
    for c in np.linspace(100, 150, 60):
        up.update(c)
    assert up.vote() == 1
    down = Breakout(40)
    for c in np.linspace(150, 100, 60):
        down.update(c)
    assert down.vote() == -1


def test_carry():
    c = Carry(funding_spread=0.005)
    d0 = date(2010, 1, 1)
    # quarterly 1% payments (days 0, 91, 182, 273, 364); funding 2% + 0.5%
    for day in range(0, 366):
        if day % 91 == 0:
            c.add_distribution(d0 + timedelta(days=day), 1.0, 100.0)
        c.update(d0 + timedelta(days=day), 0.02, 0.10)
        if day == 364:
            assert not c.ready                   # less than a full year observed
    assert c.ready
    # day 0 has rolled out of the 365-day window -> 4 payments = 4% yield
    assert abs(c.distribution_yield() - 0.04) < 1e-12
    assert abs(c.carry() - 0.015) < 1e-12 and c.vote() == 1
    assert abs(c.raw() - 0.015 / 0.10) < 1e-12
    # funding above yield -> short vote (e.g. gold: no distributions at all)
    g = Carry()
    for day in range(400):
        g.update(d0 + timedelta(days=day), 0.03, 0.15)
    assert g.carry() == -0.03 and g.vote() == -1


def test_weights():
    rules = build_rules(DEFAULT_RULES)
    w = rule_weights(rules)
    assert abs(sum(w.values()) - 1) < 1e-12
    assert abs(w["mac16_64"] - 0.0625) < 1e-12 and abs(w["breakout40"] - 0.0625) < 1e-12
    assert abs(w["carry"] - 0.5) < 1e-12
    assert rule_weights(build_rules("mac16")) == {"mac16_64": 1.0}
    w2 = rule_weights(build_rules("mac16,mac64,carry"))       # families renormalise
    assert abs(w2["carry"] - 0.5 / 0.75) < 1e-12


def test_ties_only_when_all_trend_rules_oppose_carry():
    w = rule_weights(build_rules(DEFAULT_RULES))
    names = list(w)
    ties = []
    for votes in itertools.product([-1, 1], repeat=len(names)):
        v = sum(w[n] * x for n, x in zip(names, votes))
        if abs(v) < 1e-9:
            ties.append(dict(zip(names, votes)))
    assert len(ties) == 2
    for t in ties:
        trend = {t[n] for n in names if n != "carry"}
        assert len(trend) == 1 and trend != {t["carry"]}


def test_tie_means_no_new_trade():
    s = MultiRuleSystem()
    for i, c in enumerate(PX[:400]):
        s.add_close(c, DATES[i], 0.02)
    assert s.ready and s.carry_rule.ready
    trend = [r for r in s.price_rules]
    for r in trend:
        r.vote = lambda: 1
    s.carry_rule.vote = lambda: -1
    assert s.combined_vote() == 0 and s.signal() == 0 and s.wants_to_open() == 0
    s.carry_rule.vote = lambda: 1
    assert s.signal() == 1 and s.wants_to_open() == 1


def test_mac16_only_reproduces_stage2():
    for seed in range(5):
        px = synthetic_prices(seed)
        a = run(StarterSystem(signal_type="sma"), px)
        b = run(MultiRuleSystem(rules_spec="mac16", ma_type="sma"), px)
        assert a == b and len(a) > 5, seed
        a = run(StarterSystem(signal_type="ewma"), px)
        b = run(MultiRuleSystem(rules_spec="mac16", ma_type="ewma"), px)
        assert a == b, seed


def test_readiness_and_carry_renormalisation():
    s = MultiRuleSystem()
    for i, c in enumerate(PX[:319]):
        s.add_close(c, DATES[i], 0.02)
    assert not s.ready                                   # breakout320 not ready yet
    s.add_close(PX[319], DATES[319], 0.02)
    assert s.ready and not s.carry_rule.ready            # 320 calendar days < 365
    v_price_only = s.combined_vote()
    votes = s.votes()
    exp = sum(v for k, v in votes.items() if k != "carry") / 8
    assert abs(v_price_only - exp) < 1e-12
    for i in range(320, 400):
        s.add_close(PX[i], DATES[i], 0.02)
    assert s.carry_rule.ready and s.carry_rule.vote() == -1   # no distributions, 2% funding


def test_full_system_runs_and_obeys_reentry_rule():
    dists = {i: (0.5, float(PX[i])) for i in range(0, len(PX), 91)}   # ~2% yield
    trades = run(MultiRuleSystem(), PX, rf=0.01, distributions=dists)
    assert trades
    stage2 = run(StarterSystem(), PX)
    print(f"  synthetic path: Stage 2 {len(stage2)} trades, Stage 3 {len(trades)} trades")


def test_wait_for_flip_skips_first_signal():
    for cls, kw in [(StarterSystem, {}), (MultiRuleSystem, {"rules_spec": "mac16"})]:
        s = cls(wait_for_flip=True, **kw)
        sig0, first_open = None, None
        for i, c in enumerate(PX):
            if isinstance(s, MultiRuleSystem):
                s.add_close(c, DATES[i], 0.02)
            else:
                s.add_close(c)
            if not s.ready:
                continue
            if sig0 is None:
                sig0 = s.signal()
            d = s.wants_to_open()
            if d:
                first_open = d
                break
        assert first_open == -sig0, cls      # first trade only after the signal flips


def test_wait_for_flip_parity_with_stage2():
    for seed in range(5):
        px = synthetic_prices(seed)
        a = run(StarterSystem(wait_for_flip=True), px)
        b = run(MultiRuleSystem(rules_spec="mac16", wait_for_flip=True), px)
        assert a == b and len(a) > 5, seed


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all Stage 3 checks passed")
