"""Offline demo: with carry at 50% weight and a carry sign that never changes, the
flip-to-re-enter rule locks Stage 3 out. Synthetic prices, 20 paths of ~18 years,
carry always positive. Run: python research/reentry_lockout_sim.py"""
import sys, numpy as np
import os
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tests")); sys.path.insert(0, os.path.join(ROOT, "carver_lib"))
from test_stage3 import synthetic_prices, DATES, run
from starter_core import StarterSystem
from combine import MultiRuleSystem
import test_stage3 as T
from datetime import date, timedelta
res = {}
for seed in range(20):
    px = synthetic_prices(seed, n_regimes=45, regime_len=100)   # 4500 days ~ 18y
    T.DATES[:] = [date(2005,1,3)+timedelta(days=int(i*365/252)) for i in range(len(px))]
    dists = {i: (0.5, float(px[i])) for i in range(0, len(px), 63)}   # ~2% yield vs 1% funding: carry >0 always
    cfgs = {"stage2": StarterSystem(wait_for_flip=True),
            "trend_only": MultiRuleSystem(rules_spec="mac8,mac16,mac32,mac64,breakout40,breakout80,breakout160,breakout320", wait_for_flip=True),
            "30/30/40": MultiRuleSystem(family_weights={"mac":.3,"breakout":.3,"carry":.4}, wait_for_flip=True),
            "25/25/50": MultiRuleSystem(family_weights={"mac":.25,"breakout":.25,"carry":.5}, wait_for_flip=True)}
    for k, s in cfgs.items():
        t = run(s, px, rf=0.01, distributions=dists)
        res.setdefault(k, []).append(len(t))
for k, v in res.items(): print(f"{k:10s} trades per 18y path: median {np.median(v):.0f}  (min {min(v)}, max {max(v)})")
