from datetime import date

import numpy as np
import pandas as pd
import pytest

from analysis import backtest, calendar_rules as cal, correlation as corr


class TestCalendar:
    def test_us_expiry_is_third_friday(self):
        assert cal.us_option_expiry(2026, 9) == date(2026, 9, 18)
        assert cal.us_option_expiry(2026, 12) == date(2026, 12, 18)
        assert cal.us_option_expiry(2026, 8) == date(2026, 8, 21)

    def test_kr_expiry_is_second_thursday(self):
        d = cal.kr_option_expiry(2026, 9)
        assert d.weekday() == 3
        assert 8 <= d.day <= 14

    def test_quadruple_witching_only_in_quarter_months(self):
        assert cal.is_quadruple_witching(date(2026, 9, 18))
        assert cal.is_quadruple_witching(date(2026, 12, 18))
        assert not cal.is_quadruple_witching(date(2026, 8, 21))
        assert not cal.is_quadruple_witching(date(2026, 9, 17))

    def test_buyback_period_detected(self):
        labels = [e.label for e in cal.events_for(date(2026, 9, 1))]
        assert any("자사주" in l for l in labels)
        labels = [e.label for e in cal.events_for(date(2026, 12, 1))]
        assert not any("자사주" in l for l in labels)

    def test_upcoming_is_sorted(self):
        evs = cal.upcoming(date(2026, 9, 1), 60)
        assert evs == sorted(evs, key=lambda e: e.date)


def _series(vals, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(vals))
    return pd.Series(vals, index=idx, dtype=float)


class TestCorrelation:
    def test_perfect_correlation(self):
        base = np.cumprod(1 + np.random.RandomState(0).normal(0, .01, 80)) * 100
        a = _series(base)
        r = corr.correlation(a, a * 2, window=60)
        assert r.coefficient == pytest.approx(1.0, abs=1e-6)
        assert r.reliable

    def test_small_sample_flagged_unreliable(self):
        a = _series([100, 101, 102, 103, 104])
        assert not corr.correlation(a, a, window=60).reliable

    def test_beta_scales(self):
        rs = np.random.RandomState(1).normal(0, .01, 100)
        bench = _series(np.cumprod(1 + rs) * 100)
        target = _series(np.cumprod(1 + rs * 2) * 100)
        assert corr.beta(target, bench, 90) == pytest.approx(2.0, rel=.15)


class TestBacktest:
    def test_reports_worst_and_sample_size(self):
        r = backtest.BacktestResult("t", [1.0, 2.0, -5.0])
        assert r.n == 3
        assert r.worst == -5.0
        assert r.win_rate == pytest.approx(66.67, abs=.1)
        assert not r.reliable
        assert "표본" in r.report()
        assert "최대손실" in r.report()

    def test_small_sample_warning_in_report(self):
        assert "부족" in backtest.BacktestResult("t", [1.0] * 5).report()

    def test_event_window(self):
        prices = _series(list(range(100, 140)))
        ev = prices.index[20].date()
        r = backtest.event_window_returns(prices, [ev], -10, -1)
        assert r.n == 1
        assert r.trades[0] > 0
