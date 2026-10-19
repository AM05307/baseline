"""analysis/signal_backtest.py 테스트. 합성 데이터만 사용."""

from __future__ import annotations

import pandas as pd
import pytest

from analysis import signal_backtest as sb


def _series(vals, start="2026-01-01") -> pd.Series:
    idx = pd.bdate_range(start, periods=len(vals))
    return pd.Series(vals, index=idx, dtype=float)


class TestForwardReturns:
    def test_known_values(self):
        close = _series([100, 110, 90, 105, 100])
        fwd1 = sb.forward_returns(close, 1)
        assert fwd1.iloc[0] == pytest.approx(10.0)
        assert fwd1.iloc[1] == pytest.approx(-100 * (1 - 90 / 110))
        assert pd.isna(fwd1.iloc[-1])

    def test_horizon_two(self):
        close = _series([100, 110, 90, 120])
        fwd2 = sb.forward_returns(close, 2)
        assert fwd2.iloc[0] == pytest.approx((90 / 100 - 1) * 100)
        assert fwd2.iloc[1] == pytest.approx((120 / 110 - 1) * 100)


class TestClusterEvents:
    def test_only_first_day_of_each_run_is_true(self):
        is_on = pd.Series([0, 1, 1, 1, 0, 1, 0, 1, 1], index=pd.bdate_range("2026-01-01", periods=9))
        events = sb.cluster_events(is_on)
        assert list(events) == [False, True, False, False, False, True, False, True, False]
        assert events.sum() == 3  # 연속 구간 3개(길이 3,1,2)

    def test_all_zero_has_no_events(self):
        is_on = pd.Series([0, 0, 0], index=pd.bdate_range("2026-01-01", periods=3))
        assert sb.cluster_events(is_on).sum() == 0


class TestEvaluateSignal:
    def test_risk_signal_hit_when_return_negative(self):
        idx = pd.bdate_range("2026-01-01", periods=40)
        # 신호가 on인 날 다음 horizon일 수익률이 전부 음수가 되도록 하락 구간을 만듦
        close = pd.Series([100 - i for i in range(40)], index=idx, dtype=float)
        is_on = pd.Series(1, index=idx)
        result = sb.evaluate_signal(is_on, close, direction="risk", horizons=(5,))
        full = result[5]["full"]
        assert full.n > 0
        assert full.hit_rate_pct == pytest.approx(100.0)

    def test_opportunity_signal_hit_when_return_positive(self):
        idx = pd.bdate_range("2026-01-01", periods=40)
        close = pd.Series([100 + i for i in range(40)], index=idx, dtype=float)
        is_on = pd.Series(1, index=idx)
        result = sb.evaluate_signal(is_on, close, direction="opportunity", horizons=(5,))
        full = result[5]["full"]
        assert full.hit_rate_pct == pytest.approx(100.0)

    def test_event_clustering_reduces_sample_count(self):
        idx = pd.bdate_range("2026-01-01", periods=40)
        close = pd.Series([100 + i for i in range(40)], index=idx, dtype=float)
        # 5일씩 묶어서 on -> 연속 구간들, 이벤트로 묶으면 표본이 줄어야 함
        pattern = ([1, 1, 1, 1, 1, 0, 0] * 6)[:40]
        is_on = pd.Series(pattern, index=idx)
        result = sb.evaluate_signal(is_on, close, direction="opportunity", horizons=(5,))
        full = result[5]["full"]
        assert full.n_events < full.n

    def test_reliable_flag_at_min_samples_boundary(self):
        idx = pd.bdate_range("2026-01-01", periods=100)
        close = pd.Series([100 + i * 0.1 for i in range(100)], index=idx, dtype=float)
        is_on_enough = pd.Series(1, index=idx)
        result = sb.evaluate_signal(is_on_enough, close, direction="opportunity", horizons=(5,))
        assert result[5]["full"].reliable is True

        is_on_sparse = pd.Series(0, index=idx)
        is_on_sparse.iloc[:5] = 1  # 표본 5건 << 30
        result_sparse = sb.evaluate_signal(is_on_sparse, close, direction="opportunity", horizons=(5,))
        assert result_sparse[5]["full"].reliable is False

    def test_out_of_sample_is_recent_year_only(self):
        idx = pd.bdate_range("2020-01-01", periods=1000)
        close = pd.Series([100.0 + i * 0.01 for i in range(1000)], index=idx)
        is_on = pd.Series(0, index=idx)
        is_on.iloc[10] = 1    # 오래된 구간
        is_on.iloc[-10] = 1   # 최근 1년 안
        result = sb.evaluate_signal(is_on, close, direction="opportunity", horizons=(5,), out_of_sample_years=1)
        assert result[5]["out_of_sample"].n == 1
        assert result[5]["in_sample"].n == 1
        assert result[5]["full"].n == 2

    def test_worst_pct_is_minimum_observed_return(self):
        idx = pd.bdate_range("2026-01-01", periods=10)
        close = pd.Series([100, 90, 95, 80, 85, 86, 87, 88, 89, 90], index=idx, dtype=float)
        is_on = pd.Series(1, index=idx)
        result = sb.evaluate_signal(is_on, close, direction="risk", horizons=(1,))
        full = result[1]["full"]
        returns = sb.forward_returns(close, 1).dropna()
        assert full.worst_pct == pytest.approx(float(returns.min()))
