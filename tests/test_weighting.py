"""analysis/weighting.py 테스트 — 특히 상한·재배분·정규화가 실제로 수렴하는지."""

from __future__ import annotations

import pytest

from analysis import weighting as wt


class TestRawScore:
    def test_zero_when_no_samples(self):
        assert wt.raw_score(90.0, 0) == 0.0

    def test_zero_when_hit_rate_at_or_below_half(self):
        assert wt.raw_score(50.0, 100) == 0.0
        assert wt.raw_score(40.0, 100) == 0.0

    def test_positive_above_half_and_shrinks_with_few_samples(self):
        many = wt.raw_score(70.0, 1000)
        few = wt.raw_score(70.0, 10)
        assert many > few > 0
        assert few == pytest.approx((0.70 - 0.5) * (10 / 40))

    def test_nan_hit_rate_is_zero(self):
        assert wt.raw_score(float("nan"), 50) == 0.0


class TestComputeWeightsInitialState:
    def test_equal_weight_when_no_history(self):
        hit_rates = {"A": float("nan"), "B": float("nan"), "C": float("nan")}
        sizes = {"A": 0, "B": 0, "C": 0}
        expert_of = {"A": "X", "B": "X", "C": "Y"}
        weights = wt.compute_weights(hit_rates, sizes, expert_of, expert_cap_pct=100.0, default_cap_pct=100.0)
        assert weights["A"] == pytest.approx(weights["B"]) == pytest.approx(weights["C"])
        assert sum(weights.values()) == pytest.approx(100.0)

    def test_equal_weight_when_all_at_or_below_coin_flip(self):
        hit_rates = {"A": 50.0, "B": 45.0}
        sizes = {"A": 100, "B": 100}
        expert_of = {"A": "X", "B": "Y"}
        weights = wt.compute_weights(hit_rates, sizes, expert_of, expert_cap_pct=100.0, default_cap_pct=100.0)
        assert weights["A"] == pytest.approx(50.0)
        assert weights["B"] == pytest.approx(50.0)


class TestSignalCap:
    def test_no_signal_exceeds_its_cap(self):
        # 신호 A가 압도적으로 raw score가 높아도 cap(30%)을 넘지 않아야 한다
        hit_rates = {"A": 95.0, "B": 51.0, "C": 51.0, "D": 51.0}
        sizes = {"A": 1000, "B": 50, "C": 50, "D": 50}
        expert_of = {"A": "X", "B": "Y", "C": "Y", "D": "Y"}
        weights = wt.compute_weights(hit_rates, sizes, expert_of, default_cap_pct=30.0, expert_cap_pct=100.0)
        for sid, w in weights.items():
            assert w <= 30.0 + 1e-6
        assert sum(weights.values()) == pytest.approx(100.0, abs=0.01)

    def test_signal_specific_cap_override(self):
        hit_rates = {"KSJ_FOREIGN_SELL": 95.0, "OTHER": 51.0}
        sizes = {"KSJ_FOREIGN_SELL": 1000, "OTHER": 1000}
        expert_of = {"KSJ_FOREIGN_SELL": "KSJ", "OTHER": "ZZ"}
        weights = wt.compute_weights(
            hit_rates, sizes, expert_of,
            signal_caps_pct={"KSJ_FOREIGN_SELL": 15.0}, expert_cap_pct=100.0,
        )
        assert weights["KSJ_FOREIGN_SELL"] <= 15.0 + 1e-6


class TestExpertCap:
    def test_no_expert_total_exceeds_its_cap(self):
        # 전문가 3명(X,Y,Z) 중 X,Y는 강하고 Z는 약함 -> X,Y는 40%로 묶이고
        # 남는 몫(20%)은 Z로 흡수되어 합계가 100%에 도달해야 한다.
        hit_rates = {f"X{i}": 90.0 for i in range(4)}
        hit_rates.update({f"Y{i}": 90.0 for i in range(4)})
        hit_rates["Z1"] = 55.0
        sizes = {k: 500 for k in hit_rates}
        expert_of = {
            **{f"X{i}": "X" for i in range(4)}, **{f"Y{i}": "Y" for i in range(4)}, "Z1": "Z",
        }
        weights = wt.compute_weights(hit_rates, sizes, expert_of, default_cap_pct=100.0, expert_cap_pct=40.0)
        x_total = sum(w for sid, w in weights.items() if expert_of[sid] == "X")
        y_total = sum(w for sid, w in weights.items() if expert_of[sid] == "Y")
        assert x_total == pytest.approx(40.0, abs=0.01)
        assert y_total == pytest.approx(40.0, abs=0.01)
        assert sum(weights.values()) == pytest.approx(100.0, abs=0.01)

    def test_total_falls_short_of_100_when_all_experts_are_capped(self):
        # 전문가가 딱 2명뿐이고 둘 다 40% cap에 묶이면, 100%를 채울 곳이
        # 없어서 합계가 80%에서 멈추는 게 맞다(억지로 101%를 만들지 않음).
        hit_rates = {"X1": 90.0, "Y1": 90.0}
        sizes = {"X1": 500, "Y1": 500}
        expert_of = {"X1": "X", "Y1": "Y"}
        weights = wt.compute_weights(hit_rates, sizes, expert_of, default_cap_pct=100.0, expert_cap_pct=40.0)
        assert weights["X1"] == pytest.approx(40.0, abs=0.01)
        assert weights["Y1"] == pytest.approx(40.0, abs=0.01)
        assert sum(weights.values()) == pytest.approx(80.0, abs=0.01)

    def test_weak_experts_absorb_redistributed_weight(self):
        # 전문가 X가 매우 강하고 Y,Z는 약함 -> X는 40%로 묶이고 남는 건 Y,Z로 간다
        hit_rates = {"X1": 95.0, "Y1": 55.0, "Z1": 55.0}
        sizes = {"X1": 1000, "Y1": 1000, "Z1": 1000}
        expert_of = {"X1": "X", "Y1": "Y", "Z1": "Z"}
        weights = wt.compute_weights(hit_rates, sizes, expert_of, default_cap_pct=100.0, expert_cap_pct=40.0)
        assert weights["X1"] == pytest.approx(40.0, abs=0.01)
        assert weights["Y1"] > 0 and weights["Z1"] > 0


class TestBothCapsTogether:
    def test_signal_and_expert_caps_both_respected_simultaneously(self):
        hit_rates = {f"K{i}": 95.0 for i in range(5)}
        sizes = {k: 1000 for k in hit_rates}
        expert_of = {k: "K" for k in hit_rates}
        weights = wt.compute_weights(hit_rates, sizes, expert_of, default_cap_pct=30.0, expert_cap_pct=40.0)
        for sid, w in weights.items():
            assert w <= 30.0 + 1e-6
        assert sum(weights.values()) <= 40.0 + 1e-6 + 1e-6


class TestExpertWeightTotals:
    def test_sums_per_expert(self):
        weights = {"A": 10.0, "B": 15.0, "C": 5.0}
        expert_of = {"A": "X", "B": "X", "C": "Y"}
        totals = wt.expert_weight_totals(weights, expert_of)
        assert totals == {"X": pytest.approx(25.0), "Y": pytest.approx(5.0)}
