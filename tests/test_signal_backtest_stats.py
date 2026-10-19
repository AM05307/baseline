"""
analysis/signal_backtest.py의 통계 검증 확장(base_rate·binomial_p_value·
bootstrap_ci·benjamini_hochberg) 테스트. 합성 데이터만 사용.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis import signal_backtest as sb


def _series(vals, start="2026-01-01") -> pd.Series:
    idx = pd.bdate_range(start, periods=len(vals))
    return pd.Series(vals, index=idx, dtype=float)


class TestBaseRate:
    def test_all_up_days_gives_100pct_opportunity_base_rate(self):
        returns = pd.Series([1.0, 2.0, 0.5, 3.0])
        assert sb.base_rate(returns, "opportunity") == pytest.approx(100.0)
        assert sb.base_rate(returns, "risk") == pytest.approx(0.0)

    def test_mixed_returns(self):
        returns = pd.Series([1.0, -1.0, 2.0, -3.0])  # 2/4 음수
        assert sb.base_rate(returns, "risk") == pytest.approx(50.0)

    def test_empty_returns_is_nan(self):
        assert sb.base_rate(pd.Series(dtype=float), "risk") != sb.base_rate(pd.Series(dtype=float), "risk")  # NaN


class TestBinomialPValue:
    def test_hit_rate_matching_base_rate_is_not_significant(self):
        # 100번 중 50번 적중, 기준률도 50% -> 유의하게 높다고 볼 수 없음(p 큼)
        p = sb.binomial_p_value(n_events=100, hit_count=50, base_rate_frac=0.5)
        assert p > 0.4

    def test_hit_rate_far_above_base_rate_is_significant(self):
        # 100번 중 90번 적중, 기준률 50% -> 뚜렷하게 유의
        p = sb.binomial_p_value(n_events=100, hit_count=90, base_rate_frac=0.5)
        assert p < 0.001

    def test_invalid_base_rate_returns_nan(self):
        assert sb.binomial_p_value(10, 5, base_rate_frac=0.0) != sb.binomial_p_value(10, 5, 0.0)  # NaN
        assert sb.binomial_p_value(0, 0, base_rate_frac=0.5) != sb.binomial_p_value(0, 0, 0.5)  # NaN


class TestBootstrapCi:
    def test_ci_width_shrinks_with_more_events(self):
        rs = np.random.RandomState(0)
        small = pd.Series(rs.choice([1.0, -1.0], size=20, p=[0.6, 0.4]))
        large = pd.Series(rs.choice([1.0, -1.0], size=2000, p=[0.6, 0.4]))
        lo_s, hi_s = sb.bootstrap_ci(small, "opportunity", n_boot=1000)
        lo_l, hi_l = sb.bootstrap_ci(large, "opportunity", n_boot=1000)
        assert (hi_s - lo_s) > (hi_l - lo_l)

    def test_empty_events_returns_nan(self):
        lo, hi = sb.bootstrap_ci(pd.Series(dtype=float), "risk")
        assert lo != lo and hi != hi  # NaN

    def test_ci_brackets_the_true_rate_for_large_sample(self):
        rs = np.random.RandomState(1)
        sample = pd.Series(rs.choice([1.0, -1.0], size=5000, p=[0.7, 0.3]))
        lo, hi = sb.bootstrap_ci(sample, "opportunity", n_boot=2000)
        assert lo < 70.0 < hi


class TestBenjaminiHochberg:
    def test_all_significant_p_values_stay_significant(self):
        p_values = {"A": 0.001, "B": 0.002, "C": 0.001}
        q = sb.benjamini_hochberg(p_values)
        for v in q.values():
            assert v < 0.05

    def test_correction_increases_q_value_relative_to_p_value(self):
        # 16개 중 1개만 약하게 유의한 경우, 보정 후엔 유의성이 옅어져야 한다
        p_values = {f"S{i}": 0.5 for i in range(15)}
        p_values["WEAK"] = 0.04
        q = sb.benjamini_hochberg(p_values)
        assert q["WEAK"] > 0.04

    def test_nan_p_values_pass_through_as_nan(self):
        q = sb.benjamini_hochberg({"A": 0.01, "B": float("nan")})
        assert q["B"] != q["B"]
        assert q["A"] == q["A"]

    def test_monotonic_non_decreasing_order(self):
        p_values = {"A": 0.001, "B": 0.01, "C": 0.1, "D": 0.5}
        q = sb.benjamini_hochberg(p_values)
        ordered = [q[k] for k in ["A", "B", "C", "D"]]
        assert ordered == sorted(ordered)


def _mixed_market_with_real_edge_signal(seed: int = 7, periods: int = 300):
    """상승/하락이 섞인 합성 시장 + '5일 뒤 수익률 상위 30%'인 날만 미리
    표시한 신호. base_rate가 100%로 퇴화하지 않으면서도 뚜렷한 edge가
    있는, 통계 함수 테스트용 데이터(실제 트레이딩 신호가 아니라 미래
    결과로 라벨을 만든 합성 테스트 픽스처 — 미리보기 금지 원칙은
    analysis/signals.py(신호 생성)에 적용되는 것이고, 여긴 통계 검증
    함수 자체를 검증하는 테스트 데이터 생성일 뿐이다)."""
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range("2026-01-01", periods=periods)
    rets = rs.normal(0.0002, 0.01, periods)
    close = pd.Series(100 * np.cumprod(1 + rets), index=idx)
    fwd = sb.forward_returns(close, 5)
    threshold = fwd.quantile(0.7)
    is_on = (fwd > threshold).fillna(False).astype(int)
    return close, is_on


class TestApplyBenjaminiHochberg:
    def test_mutates_q_value_in_place(self):
        close, is_on = _mixed_market_with_real_edge_signal()
        results = []
        for direction in ("opportunity",) * 3:
            perf = sb.evaluate_signal(is_on, close, direction=direction, horizons=(5,))
            results.append(perf[5]["full"])
        sb.apply_benjamini_hochberg(results)
        for r in results:
            assert r.q_value == r.q_value  # NaN 아님(p_value가 있으므로)


class TestEvaluateSignalSignificanceFields:
    def test_random_like_signal_is_not_significant(self):
        # 완전히 무작위(동전던지기)인 신호는 유의하지 않아야 한다
        rs = np.random.RandomState(42)
        idx = pd.bdate_range("2026-01-01", periods=500)
        rets = rs.normal(0, 0.01, 500)
        close = pd.Series(100 * np.cumprod(1 + rets), index=idx)
        is_on = pd.Series(rs.randint(0, 2, 500), index=idx)  # 가격과 무관한 무작위 on/off

        perf = sb.evaluate_signal(is_on, close, direction="opportunity", horizons=(20,), min_events=30)
        full = perf[20]["full"]
        assert full.significant is False

    def test_strong_trend_signal_is_significant(self):
        close, is_on = _mixed_market_with_real_edge_signal(seed=3, periods=400)
        perf = sb.evaluate_signal(is_on, close, direction="opportunity", horizons=(5,), min_events=1)
        full = perf[5]["full"]
        assert full.edge_pct > 0
        assert full.significant is True
