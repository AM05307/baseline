"""
analysis/risk_score_history.py 테스트 — 합성 데이터로만 검증(DB 비의존,
analysis/ 모듈 관례 그대로).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis import risk_score_history as rsh


def _dates(n, start="2026-01-01"):
    return pd.bdate_range(start, periods=n)


class TestWeightsAsOf:
    def test_uses_most_recent_recompute_before_date(self):
        idx = _dates(5)
        wh = pd.DataFrame({
            "recomputed_on": pd.to_datetime(["2026-01-02", "2026-01-02", "2026-01-06"]),
            "signal_id": ["A", "B", "A"],
            "new_weight_pct": [10.0, 20.0, 15.0],
        })
        out = rsh.weights_as_of(wh, idx, ["A", "B"])
        # idx[0]=01-01: A의 recomputed_on(01-02)보다 이전 -> NaN
        assert pd.isna(out.loc[idx[0], "A"])
        # idx[1]=01-02: 01-02 기록 반영
        assert out.loc[idx[1], "A"] == 10.0
        assert out.loc[idx[1], "B"] == 20.0
        # idx[4]=01-07(평일 기준 5번째): 01-06 기록으로 갱신, B는 그대로 20
        assert out.loc[idx[4], "A"] == 15.0
        assert out.loc[idx[4], "B"] == 20.0

    def test_signal_never_recomputed_is_all_nan(self):
        idx = _dates(3)
        wh = pd.DataFrame({
            "recomputed_on": pd.to_datetime(["2026-01-02"]),
            "signal_id": ["A"], "new_weight_pct": [10.0],
        })
        out = rsh.weights_as_of(wh, idx, ["A", "NEVER"])
        assert out["NEVER"].isna().all()

    def test_empty_history_returns_all_nan(self):
        idx = _dates(3)
        out = rsh.weights_as_of(pd.DataFrame(columns=["recomputed_on", "signal_id", "new_weight_pct"]), idx, ["A"])
        assert out["A"].isna().all()


class TestEffectiveWeightsAsOf:
    def test_cold_start_fills_equal_weight(self):
        idx = _dates(3)
        empty_wh = pd.DataFrame(columns=["recomputed_on", "signal_id", "new_weight_pct"])
        filled, cold_start = rsh.effective_weights_as_of(empty_wh, idx, ["A", "B", "C", "D"])
        assert (filled == 25.0).all().all()
        assert cold_start.all()

    def test_marks_only_fully_cold_dates(self):
        idx = _dates(3)
        wh = pd.DataFrame({
            "recomputed_on": pd.to_datetime(["2026-01-02"]),
            "signal_id": ["A"], "new_weight_pct": [10.0],
        })
        filled, cold_start = rsh.effective_weights_as_of(wh, idx, ["A", "B"])
        # idx[0]: A도 NaN -> 둘 다 냉시작 평균(50)으로 채워짐, cold_start True
        assert cold_start.iloc[0] is True or cold_start.iloc[0] == True  # noqa: E712
        # idx[1],idx[2]: A는 실제값(10)으로 채워지고 B는 NaN->50(cold fill) 섞임
        # -> "전체가 냉시작"은 아니므로 cold_start는 False
        assert cold_start.iloc[1] == False  # noqa: E712
        assert filled.loc[idx[1], "A"] == 10.0
        assert filled.loc[idx[1], "B"] == 50.0  # 100/2


class TestReconstructScoreSeries:
    def test_matches_manual_calc_for_single_risk_signal(self):
        idx = _dates(2)
        is_on = pd.DataFrame({"R": [1, 0]}, index=idx)
        weights = pd.DataFrame({"R": [30.0, 30.0]}, index=idx)
        score = rsh.reconstruct_score_series(is_on, weights, {"R": "risk"})
        assert score.iloc[0] == pytest.approx((30 + 100) / 2)  # 65
        assert score.iloc[1] == pytest.approx(50.0)  # off -> 중립

    def test_legacy_opportunity_signal_lowers_score(self):
        idx = _dates(1)
        is_on = pd.DataFrame({"O": [1]}, index=idx)
        weights = pd.DataFrame({"O": [40.0]}, index=idx)
        score = rsh.reconstruct_score_series(is_on, weights, {"O": "opportunity"}, net_opportunity=True)
        assert score.iloc[0] == pytest.approx((-40 + 100) / 2)  # 30

    def test_separated_opportunity_goes_to_its_own_series(self):
        idx = _dates(1)
        is_on = pd.DataFrame({"O": [1], "R": [1]}, index=idx)
        weights = pd.DataFrame({"O": [40.0], "R": [20.0]}, index=idx)
        d = {"O": "opportunity", "R": "risk"}
        assert rsh.reconstruct_score_series(is_on, weights, d).iloc[0] == pytest.approx(60.0)
        assert rsh.reconstruct_opportunity_series(is_on, weights, d).iloc[0] == pytest.approx(70.0)

    def test_clips_to_0_100_range(self):
        idx = _dates(1)
        is_on = pd.DataFrame({"R1": [1], "R2": [1]}, index=idx)
        weights = pd.DataFrame({"R1": [100.0], "R2": [100.0]}, index=idx)
        score = rsh.reconstruct_score_series(is_on, weights, {"R1": "risk", "R2": "risk"})
        assert score.iloc[0] == 100.0

    def test_no_common_signals_returns_neutral_50(self):
        idx = _dates(2)
        is_on = pd.DataFrame({"X": [1, 0]}, index=idx)
        weights = pd.DataFrame({"Y": [10.0, 10.0]}, index=idx)
        score = rsh.reconstruct_score_series(is_on, weights, {"X": "risk"})
        assert (score == 50.0).all()


class TestBucketForwardReturns:
    def test_counts_and_stats_per_bucket(self):
        idx = _dates(10)
        scores = pd.Series([10, 10, 50, 50, 50, 90, 90, 90, 90, 90], index=idx, dtype=float)
        fwd5 = pd.Series([1, 2, -1, -2, 3, -10, -5, -20, 1, -1], index=idx, dtype=float)
        results = rsh.bucket_forward_returns(scores, {5: fwd5}, buckets=[(0, 40), (40, 60), (60, 101)], min_samples=3)
        by_label = {r.bucket_label: r for r in results}

        assert by_label["0~40"].n == 2
        assert by_label["40~60"].n == 3
        assert by_label["60~100"].n == 5
        assert by_label["60~100"].worst_return_pct == -20.0
        assert by_label["60~100"].reliable is True  # n=5 >= min_samples=3
        assert by_label["0~40"].reliable is False  # n=2 < 3

    def test_nan_rows_dropped_before_bucketing(self):
        idx = _dates(3)
        scores = pd.Series([10.0, float("nan"), 10.0], index=idx)
        fwd = pd.Series([1.0, 2.0, float("nan")], index=idx)
        results = rsh.bucket_forward_returns(scores, {5: fwd}, buckets=[(0, 40)], min_samples=1)
        assert results[0].n == 1  # 둘 다 유효한 행만 1개

    def test_multiple_horizons_produce_separate_rows(self):
        idx = _dates(2)
        scores = pd.Series([10.0, 10.0], index=idx)
        fwd5 = pd.Series([1.0, 2.0], index=idx)
        fwd20 = pd.Series([5.0, 6.0], index=idx)
        results = rsh.bucket_forward_returns(scores, {5: fwd5, 20: fwd20}, buckets=[(0, 40)], min_samples=1)
        horizons = {r.horizon for r in results}
        assert horizons == {5, 20}


class TestForwardStatsByGroup:
    """기회 신호 이후 수익률을 위험 상태별로 — 이벤트 단위(연속 on 구간의 첫날)·상태 전체 기준 동반."""

    def test_events_counted_at_first_day_and_grouped_by_that_day(self):
        idx = _dates(8)
        on = pd.Series([0, 1, 1, 0, 1, 0, 0, 1], index=idx).astype(bool)
        group = pd.Series(["정상", "정상", "1단계", "1단계", "1단계", "정상", "정상", "정상"], index=idx)
        fwd = {5: pd.Series([1.0, -2.0, 9.0, 3.0, 4.0, 0.0, 0.0, float("nan")], index=idx)}
        out = {s.group: s for s in rsh.forward_stats_by_group(on, group, fwd, ["정상", "1단계"])}
        assert out["정상"].n_events == 1 and out["정상"].mean_return_pct == -2.0  # 마지막 날 이벤트는 미래 데이터 없음 → 제외
        assert out["1단계"].n_events == 1 and out["1단계"].worst_return_pct == 4.0
        assert out["1단계"].n_days == 2  # 켜진 날(겹침)은 참고용으로 따로
        assert out["정상"].group_all_days_n == 4 and out["1단계"].group_all_days_mean_pct == pytest.approx(16 / 3)
        assert not out["정상"].reliable

    def test_empty_group_has_nan_not_invented(self):
        idx = _dates(3)
        out = rsh.forward_stats_by_group(pd.Series(False, index=idx), pd.Series("정상", index=idx),
                                         {5: pd.Series(1.0, index=idx)}, ["정상", "2단계 이상"])
        assert all(s.n_events == 0 and np.isnan(s.mean_return_pct) for s in out)
