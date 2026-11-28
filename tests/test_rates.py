"""Offline checks for carver_lib/rates.py (DTB3 parsing and the lagged step lookup)."""
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "carver_lib"))
from rates import StepRate, parse_fred_csv   # noqa: E402
from rules import Carry                      # noqa: E402

FRED = """observation_date,DTB3
2008-01-02,3.17
2008-01-03,3.23
2008-01-04,.
2008-01-07,
2008-01-08,3.19
"""


def test_parse_fred_skips_missing_and_converts_percent():
    pts = parse_fred_csv(FRED)
    assert pts == [(date(2008, 1, 2), 0.0317), (date(2008, 1, 3), 0.0323), (date(2008, 1, 8), 0.0319)]
    # old FRED header and quoted fields
    assert parse_fred_csv('"DATE","DTB3"\n"2020-01-02","1.52"\n') == [(date(2020, 1, 2), 0.0152)]
    assert parse_fred_csv("<html>blocked</html>") == []


def test_step_rate_uses_one_day_publication_lag():
    r = StepRate(parse_fred_csv(FRED), lag_days=1)
    assert r.rate_at(date(2008, 1, 2)) is None          # 2 Jan value not published until 3 Jan
    assert r.rate_at(date(2008, 1, 3)) == 0.0317
    assert r.rate_at(datetime(2008, 1, 4, 16)) == 0.0323
    assert r.rate_at(date(2008, 1, 8)) == 0.0323        # missing days forward-filled
    assert r.rate_at(date(2008, 1, 9)) == 0.0319
    assert r.rate_at(date(2030, 1, 1)) == 0.0319


def test_step_rate_incremental_add_no_lag():
    r = StepRate(lag_days=0)
    r.add(date(2010, 1, 4), 0.0005)
    r.add(date(2010, 1, 5), 0.0006)
    r.add(date(2010, 1, 5), 0.0007)     # same day overwritten
    r.add(date(2010, 1, 1), 0.0004)     # out of order inserted
    assert len(r) == 3 and r.first_date == date(2010, 1, 1)
    assert r.rate_at(date(2010, 1, 5)) == 0.0007
    assert r.rate_at(date(2010, 1, 2)) == 0.0004


def test_carry_funding_is_risk_free_plus_spread():
    c = Carry(funding_spread=0.01)
    c.add_distribution(date(2010, 6, 1), 2.0, 100.0)    # 2% yield
    c.update(date(2010, 1, 1), 0.001)
    c.update(date(2011, 1, 1), 0.001)                   # DTB3-like 0.1%
    assert abs(c.carry() - (0.02 - 0.011)) < 1e-12
