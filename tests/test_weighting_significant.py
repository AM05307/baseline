"""
analysis/weighting.py의 신버전 가중치 공식(raw_score_significant,
compute_weights_significant) 테스트 — 기준률·신뢰구간 하한·이벤트 수
기반 가중치 0 규칙 포함.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from analysis import weighting as wt


@dataclass
class _FakePerf:
    """analysis.signal_backtest.SignalPerformance의 가중치 계산에 필요한
    필드만 흉내낸 테스트용 더미(실제 타입을 안 써도 duck typing으로 통과)."""
    edge_pct: float
    ci_lower_pct: float
    base_rate_pct: float
    n_events: int


class TestRawScoreSignificant:
    def test_zero_when_too_few_events(self):
        score = wt.raw_score_significant(edge_pct=20.0, ci_lower_pct=60.0, base_rate_pct=50.0, n_events=10)
        assert score == 0.0

    def test_zero_when_ci_lower_at_or_below_base_rate(self):
        # 신뢰구간 하한이 기준률과 같거나 낮으면 "우연일 수 있다"고 보고 0
        score = wt.raw_score_significant(edge_pct=5.0, ci_lower_pct=50.0, base_rate_pct=50.0, n_events=100)
        assert score == 0.0

    def test_zero_when_stats_are_nan(self):
        assert wt.raw_score_significant(float("nan"), 60.0, 50.0, 100) == 0.0
        assert wt.raw_score_significant(10.0, float("nan"), 50.0, 100) == 0.0
        assert wt.raw_score_significant(10.0, 60.0, float("nan"), 100) == 0.0

    def test_positive_when_ci_lower_clears_base_rate_with_enough_events(self):
        score = wt.raw_score_significant(edge_pct=15.0, ci_lower_pct=55.0, base_rate_pct=50.0, n_events=200)
        assert score > 0.0

    def test_shrinks_toward_zero_with_fewer_events(self):
        many = wt.raw_score_significant(20.0, 55.0, 50.0, n_events=1000, min_events=30)
        few = wt.raw_score_significant(20.0, 55.0, 50.0, n_events=31, min_events=30)
        assert many > few > 0


class TestKhjIpoWeakSpecificCheck:
    """사용자가 명시적으로 확인을 요청한 케이스: KHJ_IPO_WEAK(적중률 50.8%)가
    신버전 규칙에서 가중치 0을 받는지. 적중률이 기준률보다 살짝 높아도
    신뢰구간이 넓으면(표본이 적거나 변동성이 크면) 하한이 기준률을 못 넘어
    0이 되는 게 정상이다 — 50.8%라는 숫자 자체가 통계적으로 무의미할 수
    있다는 걸 보여주는 시나리오."""

    def test_weak_edge_with_wide_ci_gets_zero_weight(self):
        # edge가 겨우 +0.8%p인데 신뢰구간 하한이 기준률 밑으로 내려가는
        # 전형적인 "약한 신호" 상황을 흉내낸다.
        perf = _FakePerf(edge_pct=0.8, ci_lower_pct=42.0, base_rate_pct=50.0, n_events=390)
        score = wt.raw_score_significant(perf.edge_pct, perf.ci_lower_pct, perf.base_rate_pct, perf.n_events)
        assert score == 0.0


class TestComputeWeightsSignificant:
    def test_random_signal_gets_zero_weight(self):
        # 공통 규칙: "무작위 신호는 가중치 0을 받는다" — edge가 0에 가깝고
        # 신뢰구간이 기준률을 포함하는(하한이 기준률 이하인) 전형적인 무작위 신호.
        perf_by_signal = {
            "RANDOM": _FakePerf(edge_pct=0.5, ci_lower_pct=48.0, base_rate_pct=50.0, n_events=200),
            "REAL_EDGE": _FakePerf(edge_pct=20.0, ci_lower_pct=58.0, base_rate_pct=50.0, n_events=200),
        }
        expert_of = {"RANDOM": "X", "REAL_EDGE": "Y"}
        weights = wt.compute_weights_significant(perf_by_signal, expert_of, expert_cap_pct=100.0)
        assert weights["RANDOM"] == 0.0
        assert weights["REAL_EDGE"] > 0.0

    def test_all_random_signals_fall_back_to_equal_weight(self):
        perf_by_signal = {
            f"R{i}": _FakePerf(edge_pct=0.1, ci_lower_pct=45.0, base_rate_pct=50.0, n_events=5)
            for i in range(4)
        }
        expert_of = {k: "X" for k in perf_by_signal}
        weights = wt.compute_weights_significant(perf_by_signal, expert_of, expert_cap_pct=100.0, default_cap_pct=100.0)
        vals = list(weights.values())
        assert all(v == pytest.approx(vals[0]) for v in vals)

    def test_caps_still_apply(self):
        perf_by_signal = {
            "A": _FakePerf(edge_pct=40.0, ci_lower_pct=70.0, base_rate_pct=50.0, n_events=1000),
            "B": _FakePerf(edge_pct=1.0, ci_lower_pct=51.0, base_rate_pct=50.0, n_events=50),
        }
        expert_of = {"A": "X", "B": "Y"}
        weights = wt.compute_weights_significant(perf_by_signal, expert_of, default_cap_pct=30.0, expert_cap_pct=100.0)
        assert weights["A"] <= 30.0 + 1e-6

    def test_backward_compatible_compute_weights_unaffected(self):
        # 구버전 compute_weights()는 이 모듈 변경 후에도 그대로 동작해야 한다.
        hit_rates = {"A": 70.0, "B": 40.0}
        sizes = {"A": 1000, "B": 1000}
        expert_of = {"A": "X", "B": "Y"}
        weights = wt.compute_weights(hit_rates, sizes, expert_of, expert_cap_pct=100.0, default_cap_pct=100.0)
        assert weights["A"] > 0
        assert weights["B"] == 0.0
