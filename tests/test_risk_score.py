"""analysis/risk_score.py 테스트."""

from __future__ import annotations

import pytest

from analysis import risk_score as rs


class TestComputeRiskScore:
    def test_neutral_when_nothing_on(self):
        score, risk_w, opp_w = rs.compute_risk_score({}, {})
        assert score == pytest.approx(50.0)
        assert risk_w == 0 and opp_w == 0

    def test_all_risk_pushes_score_up(self):
        weights = {"A": 40.0, "B": 30.0}
        directions = {"A": "risk", "B": "risk"}
        score, risk_w, opp_w = rs.compute_risk_score(weights, directions)
        assert risk_w == pytest.approx(70.0)
        assert opp_w == 0.0
        assert score == pytest.approx((70 + 100) / 2)

    def test_legacy_netting_opportunity_pushes_score_down(self):
        weights = {"A": 40.0}
        directions = {"A": "opportunity"}
        score, risk_w, opp_w = rs.compute_risk_score(weights, directions, net_opportunity=True)
        assert opp_w == pytest.approx(40.0)
        assert score == pytest.approx((-40 + 100) / 2)

    def test_separated_opportunity_does_not_change_risk_score(self):
        """2026-10-18: opportunity 신호는 위험 점수를 깎지 않는다(기본)."""
        weights = {"R": 30.0, "O": 40.0}
        directions = {"R": "risk", "O": "opportunity"}
        score, risk_w, opp_w = rs.compute_risk_score(weights, directions)
        assert score == pytest.approx((30 + 100) / 2)
        assert (risk_w, opp_w) == (30.0, 40.0)
        assert rs.compute_opportunity_score(opp_w) == pytest.approx(70.0)

    def test_separated_status_ignores_opportunity(self):
        on = {"R": {"direction": "risk", "weight_pct": 30.0}, "O": {"direction": "opportunity", "weight_pct": 40.0}}
        sep = rs.build_risk_state(on)
        legacy = rs.build_risk_state(on, net_opportunity=True)
        assert sep.risk_score == 65.0 and sep.sell_stage_if_holding == "분할 매도 1/3 단계"
        assert legacy.risk_score == 45.0 and legacy.sell_stage_if_holding is None
        assert sep.opportunity_score == legacy.opportunity_score == 70.0

    def test_score_is_clamped_to_0_100(self):
        weights = {f"R{i}": 30.0 for i in range(10)}  # 훨씬 넘치는 risk 가중치 합
        directions = {k: "risk" for k in weights}
        score, _, _ = rs.compute_risk_score(weights, directions)
        assert score == 100.0


class TestDetermineStatus:
    def test_below_threshold_is_buyable_with_no_sell_stage(self):
        status, stage = rs.determine_status(risk_score=30.0, combo_triggered=False, risk_threshold=60.0)
        assert status == "매수 가능"
        assert stage is None

    def test_above_threshold_is_hold_with_stage_one(self):
        status, stage = rs.determine_status(
            risk_score=65.0, combo_triggered=False, risk_threshold=60.0, sell_stage_2_threshold=80.0,
        )
        assert status == "보류"
        assert stage == "분할 매도 1/3 단계"

    def test_very_high_score_is_stage_two(self):
        status, stage = rs.determine_status(
            risk_score=85.0, combo_triggered=False, risk_threshold=60.0, sell_stage_2_threshold=80.0,
        )
        assert stage == "분할 매도 2/3 단계"

    def test_combo_overrides_regardless_of_score(self):
        status, stage = rs.determine_status(risk_score=10.0, combo_triggered=True, risk_threshold=60.0)
        assert status == "보류"
        assert "KHJ_COMBO" in stage


class TestBuildRiskState:
    def test_combo_triggered_when_value_is_three(self):
        on_signals = {
            "KHJ_RATE_STAGE": {"direction": "risk", "weight_pct": 10.0},
        }
        state = rs.build_risk_state(on_signals, combo_value=3)
        assert state.combo_triggered is True
        assert "KHJ_COMBO" in state.sell_stage_if_holding

    def test_combo_not_triggered_when_value_below_three(self):
        state = rs.build_risk_state({}, combo_value=2)
        assert state.combo_triggered is False

    def test_on_signals_list_matches_input(self):
        on_signals = {
            "A": {"direction": "risk", "weight_pct": 20.0},
            "B": {"direction": "opportunity", "weight_pct": 10.0},
        }
        state = rs.build_risk_state(on_signals)
        ids = {row["signal_id"] for row in state.on_signals}
        assert ids == {"A", "B"}
        assert state.risk_weight_sum == pytest.approx(20.0)
        assert state.opportunity_weight_sum == pytest.approx(10.0)

    def test_state_includes_contributions_and_concentration_warnings(self):
        on_signals = {"A": {"direction": "risk", "weight_pct": 20.0}}
        state = rs.build_risk_state(on_signals)
        assert state.contributions == [{"signal_id": "A", "direction": "risk", "weight_pct": 20.0,
                                        "score": "risk", "score_contribution": 10.0}]
        assert state.concentration_warnings == []


class TestScoreContributions:
    def test_risk_contributes_positive_half_weight(self):
        out = rs.score_contributions({"A": {"direction": "risk", "weight_pct": 30.0}})
        assert out == [{"signal_id": "A", "direction": "risk", "weight_pct": 30.0,
                        "score": "risk", "score_contribution": 15.0}]

    def test_legacy_opportunity_contributes_negative_half_weight(self):
        out = rs.score_contributions({"A": {"direction": "opportunity", "weight_pct": 30.0}}, net_opportunity=True)
        assert out[0]["score_contribution"] == -15.0 and out[0]["score"] == "risk"

    def test_separated_opportunity_contributes_to_opportunity_score(self):
        out = rs.score_contributions({"A": {"direction": "opportunity", "weight_pct": 30.0}})
        assert out[0]["score_contribution"] == 15.0 and out[0]["score"] == "opportunity"

    def test_sorted_by_absolute_contribution_descending(self):
        on_signals = {
            "SMALL": {"direction": "risk", "weight_pct": 5.0},
            "BIG": {"direction": "opportunity", "weight_pct": 40.0},
        }
        out = rs.score_contributions(on_signals)
        assert [c["signal_id"] for c in out] == ["BIG", "SMALL"]

    def test_contributions_sum_plus_50_equals_unclamped_score(self):
        on_signals = {
            "A": {"direction": "risk", "weight_pct": 20.0},
            "B": {"direction": "opportunity", "weight_pct": 8.0},
        }
        weights = {sid: v["weight_pct"] for sid, v in on_signals.items()}
        directions = {sid: v["direction"] for sid, v in on_signals.items()}
        for net in (True, False):
            out = rs.score_contributions(on_signals, net_opportunity=net)
            score, _, opp_w = rs.compute_risk_score(weights, directions, net_opportunity=net)
            assert 50 + sum(c["score_contribution"] for c in out if c["score"] == "risk") == pytest.approx(score)
        opp = sum(c["score_contribution"] for c in out if c["score"] == "opportunity")
        assert 50 + opp == pytest.approx(rs.compute_opportunity_score(opp_w))

    def test_empty_input_returns_empty(self):
        assert rs.score_contributions({}) == []


class TestConcentrationWarnings:
    def test_warns_when_group_dominates_total_weight(self):
        on_signals = {
            "YJH_US10Y": {"direction": "risk", "weight_pct": 30.0},
            "KSJ_US10Y_HIGH": {"direction": "risk", "weight_pct": 30.0},
            "OTHER": {"direction": "risk", "weight_pct": 5.0},
        }
        warnings = rs.concentration_warnings(on_signals)
        assert len(warnings) == 1
        assert warnings[0]["group"] == "US10Y(^TNX)"
        assert set(warnings[0]["signals"]) == {"YJH_US10Y", "KSJ_US10Y_HIGH"}
        assert warnings[0]["weight_pct"] == pytest.approx(60.0)
        assert warnings[0]["share_of_total_pct"] == pytest.approx(60 / 65 * 100)

    def test_no_warning_when_only_one_group_member_is_on(self):
        on_signals = {
            "YJH_US10Y": {"direction": "risk", "weight_pct": 30.0},
            "OTHER": {"direction": "risk", "weight_pct": 5.0},
        }
        assert rs.concentration_warnings(on_signals) == []

    def test_no_warning_when_share_below_threshold(self):
        on_signals = {
            "YJH_US10Y": {"direction": "risk", "weight_pct": 10.0},
            "KSJ_US10Y_HIGH": {"direction": "risk", "weight_pct": 10.0},
            "OTHER": {"direction": "risk", "weight_pct": 80.0},
        }
        assert rs.concentration_warnings(on_signals) == []

    def test_no_signals_on_returns_no_warnings(self):
        assert rs.concentration_warnings({}) == []

    def test_custom_groups_and_threshold(self):
        on_signals = {"X": {"direction": "risk", "weight_pct": 10.0}, "Y": {"direction": "risk", "weight_pct": 10.0}}
        warnings = rs.concentration_warnings(on_signals, groups={"G": ["X", "Y"]}, ratio_threshold=0.5)
        assert len(warnings) == 1
        assert warnings[0]["weight_pct"] == pytest.approx(20.0)


class TestAttainableCeiling:
    """2026-10-18: 수동 입력 누락으로 도달할 수 없는 단계를 알리는 상한 계산."""

    W = {"R1": 25.0, "R2": 20.0, "MAN": 15.0, "KHJ_RATE_STAGE": 10.0, "KHJ_CONCENTRATION": 5.0,
         "KHJ_IPO_WEAK": 5.0, "O": 15.0}
    D = {"R1": "risk", "R2": "risk", "MAN": "risk", "KHJ_RATE_STAGE": "risk", "KHJ_CONCENTRATION": "risk",
         "KHJ_IPO_WEAK": "risk", "O": "opportunity"}

    def test_missing_manual_inputs_block_stage_2_and_3(self):
        avail = {"R1", "R2", "KHJ_CONCENTRATION", "KHJ_IPO_WEAK", "O"}
        c = rs.attainable_ceiling(self.W, self.D, avail)
        assert c["max_score"] == pytest.approx((55 + 100) / 2)  # opportunity는 상한에 안 들어감
        assert c["max_score_if_complete"] == pytest.approx((80 + 100) / 2)
        stages = {u["stage"]: u for u in c["unreachable"]}
        assert set(stages) == {"2단계", "3단계"}
        assert stages["2단계"]["missing"] == ["KHJ_RATE_STAGE", "MAN"] and not stages["2단계"]["structural"]
        assert stages["3단계"]["missing"] == ["KHJ_RATE_STAGE"]

    def test_all_available_has_no_warning(self):
        c = rs.attainable_ceiling(self.W, self.D, set(self.W))
        assert c["unreachable"] == []

    def test_structural_when_weights_cannot_reach_even_if_complete(self):
        c = rs.attainable_ceiling({"R": 10.0}, {"R": "risk"}, set(), combo_components=())
        assert {u["stage"]: u["structural"] for u in c["unreachable"]} == {"1단계": True, "2단계": True}

    def test_zero_weight_missing_signal_is_not_listed(self):
        c = rs.attainable_ceiling({"R": 100.0, "Z": 0.0}, {"R": "risk", "Z": "risk"}, {"R"}, combo_components=())
        assert c["missing_risk"] == [] and c["unreachable"] == []
