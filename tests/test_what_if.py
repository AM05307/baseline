"""
analysis/what_if.py + scripts/what_if_recent.py 테스트(합성 데이터, 네트워크·주문 없음).

핵심 두 가지(요청 그대로):
  1. 미래참조 없음 — t일 이후의 시세·신호값·weight_history를 바꿔도 t+1일까지의
     비중과 수익률이 그대로다. 해외 세션 신호는 같은 날 값을 안 쓴다.
  2. 비중 0%면 수익률이 0이다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import what_if as wi  # noqa: E402
from db import store  # noqa: E402
from scripts import what_if_recent as script  # noqa: E402

NO_COST = wi.CostModel(0.0, 0.0, lambda d: 0.0)
DIRECTIONS = {"R1": "risk", "R2": "risk", "R3": "risk", "US": "risk", "O1": "opportunity"}
STAGE_W = dict(wi.DEFAULT_STAGE_WEIGHTS)


def _values(dates, on_mask):
    return pd.DataFrame({"date": dates, "value": np.asarray(on_mask, dtype=float),
                         "is_on": np.asarray(on_mask, dtype=int)})


def _synthetic(n=40, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n)
    close = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.02, n)), index=dates)
    values = {sid: _values(dates, rng.random(n) < 0.5) for sid in DIRECTIONS}
    wh = pd.DataFrame([
        {"id": i, "recomputed_on": dates[0].strftime("%Y-%m-%d"), "signal_id": sid, "new_weight_pct": 25.0}
        for i, sid in enumerate(DIRECTIONS)
    ])
    return close, values, wh


def _targets(close, values, wh, prior=frozenset({"US"})):
    """스크립트와 같은 배선: 결정일 상태 → 다음 거래일 목표 비중."""
    dates = close.index
    is_on, _ = wi.align_signals(values, dates, set(prior))
    weights = wi.weights_for_dates(wh, dates, list(DIRECTIONS))
    states = wi.score_and_stage(is_on, weights, DIRECTIONS)
    return states["stage"].map(STAGE_W).shift(1)


# ---------------------------------------------------------------- 미래참조

class TestNoLookahead:
    @pytest.mark.parametrize("cut", [10, 20, 30])
    def test_changing_data_after_t_does_not_change_weights_through_t_plus_1(self, cut):
        close, values, wh = _synthetic()
        base = _targets(close, values, wh)
        t = close.index[cut]

        rng = np.random.default_rng(99)
        close2 = close.copy()
        close2[close2.index > t] *= rng.uniform(0.5, 1.5, (close2.index > t).sum())
        values2 = {}
        for sid, df in values.items():
            df = df.copy()
            after = df["date"] > t
            df.loc[after, "is_on"] = 1 - df.loc[after, "is_on"]
            values2[sid] = df
        wh2 = pd.concat([wh, pd.DataFrame([
            {"id": 100 + i, "recomputed_on": (t + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
             "signal_id": sid, "new_weight_pct": 90.0} for i, sid in enumerate(DIRECTIONS)
        ])])
        changed = _targets(close2, values2, wh2)

        upto = close.index[: cut + 2]  # t+1일 비중은 t일 종가 상태로 정해진다
        pd.testing.assert_series_equal(base[upto], changed[upto])

        ret, ret2 = close.pct_change(), close2.pct_change()
        dd = pd.Series(close.index, index=close.index).shift(1)
        sl = close.index[1: cut + 1]
        a = wi.simulate(ret[sl], base[sl], dd[sl], 1.0, NO_COST)
        a2 = wi.simulate(ret2[sl], changed[sl], dd[sl], 1.0, NO_COST)
        pd.testing.assert_frame_equal(a, a2)

    def test_control_changing_data_at_t_does_change_t_plus_1(self):
        """대조군: t일 값을 바꾸면 t+1 비중이 바뀌어야 한다 — 위 테스트가 빈 검사가 아님을 확인."""
        close, values, wh = _synthetic()
        dates = close.index
        for sid in ("R1", "R2", "R3"):
            values[sid]["is_on"] = 0
        values["O1"]["is_on"] = 0
        k = 15
        before = _targets(close, values, wh)
        for sid in ("R1", "R2", "R3"):
            values[sid].loc[values[sid]["date"] == dates[k], "is_on"] = 1
        after = _targets(close, values, wh)
        assert before[dates[k + 1]] == 1.0
        assert after[dates[k + 1]] < 1.0  # 리스크 3개 → 점수 87.5 → 2단계
        assert after[dates[k]] == before[dates[k]]  # 같은 날 비중은 안 바뀜(t-1 상태로 정해짐)

    def test_prior_session_signal_uses_strictly_earlier_value(self):
        dates = pd.bdate_range("2024-01-01", periods=5)
        df = _values(dates, [0, 0, 1, 0, 0])
        is_on, _ = wi.align_signals({"US": df, "KR": df}, dates, {"US"})
        assert is_on.loc[dates[2], "KR"] == 1  # 한국 장 신호: 같은 날 값 사용
        assert is_on.loc[dates[2], "US"] == 0  # 해외 세션: 같은 날 값 미사용
        assert is_on.loc[dates[3], "US"] == 1  # 다음 결정일에 반영
        assert np.isnan(is_on.loc[dates[0], "US"])  # 이전 값 없음 → 데이터 없음

    def test_stale_signal_value_becomes_missing(self):
        dates = pd.bdate_range("2024-01-01", periods=30)
        df = _values(dates[:3], [1, 1, 1])
        is_on, _ = wi.align_signals({"KR": df}, dates, set(), max_gap_days=5)
        assert is_on.loc[dates[2], "KR"] == 1
        assert np.isnan(is_on.loc[dates[-1], "KR"])

    def test_weight_history_after_date_is_not_used_and_same_day_takes_latest_id(self):
        dates = pd.bdate_range("2024-01-01", periods=6)
        wh = pd.DataFrame([
            {"id": 1, "recomputed_on": "2024-01-02", "signal_id": "R1", "new_weight_pct": 30.0},
            {"id": 2, "recomputed_on": "2024-01-02", "signal_id": "R1", "new_weight_pct": 10.0},
            {"id": 3, "recomputed_on": "2024-01-05", "signal_id": "R1", "new_weight_pct": 50.0},
        ])
        w = wi.weights_for_dates(wh, dates, ["R1"])["R1"]
        assert np.isnan(w[dates[0]])  # 기록 전 → 냉시작
        assert w[pd.Timestamp("2024-01-02")] == 10.0
        assert w[pd.Timestamp("2024-01-04")] == 10.0
        assert w[pd.Timestamp("2024-01-05")] == 50.0


# ---------------------------------------------------------------- 비중 0%·비용

class TestZeroWeightAndCosts:
    def test_zero_weight_returns_are_zero(self):
        idx = pd.bdate_range("2024-01-01", periods=10)
        ret = pd.Series(np.linspace(-0.1, 0.1, 10), index=idx)
        dd = pd.Series(idx, index=idx).shift(1)
        costs = wi.CostModel(0.001, 0.001, lambda d: 0.002)
        sim = wi.simulate(ret, pd.Series(0.0, index=idx), dd, 0.0, costs)
        assert (sim["ret"] == 0).all()
        assert (sim["equity"] == 1.0).all()
        assert sim["cost"].sum() == 0

    def test_after_moving_to_zero_only_the_trade_cost_is_lost(self):
        idx = pd.bdate_range("2024-01-01", periods=5)
        ret = pd.Series([0.05, -0.2, 0.3, -0.1, 0.07], index=idx)
        dd = pd.Series(idx, index=idx).shift(1)
        dd.iloc[0] = idx[0] - pd.Timedelta(days=1)
        costs = wi.CostModel(0.001, 0.0005, lambda d: 0.002)
        sim = wi.simulate(ret, pd.Series(0.0, index=idx), dd, 1.0, costs)
        assert sim["ret"].iloc[0] == pytest.approx(-(0.001 + 0.0005 + 0.002))
        assert (sim["ret"].iloc[1:] == 0).all()

    def test_buy_has_no_tax_sell_has_tax(self):
        idx = pd.bdate_range("2024-01-01", periods=2)
        ret = pd.Series([0.0, 0.0], index=idx)
        dd = pd.Series(idx, index=idx)
        costs = wi.CostModel(0.001, 0.0, lambda d: 0.002)
        sell = wi.simulate(ret, pd.Series([0.66, 0.66], index=idx), dd, 1.0, costs)
        assert sell["cost"].iloc[0] == pytest.approx(0.34 * 0.003)
        assert sell["changed"].sum() == 1  # 목표가 같으면 다시 거래 안 함
        buy = wi.simulate(ret, pd.Series([1.0, 1.0], index=idx), dd, 0.0, costs)
        assert buy["cost"].iloc[0] == pytest.approx(1.0 * 0.001)

    def test_full_weight_matches_underlying(self):
        idx = pd.bdate_range("2024-01-01", periods=4)
        ret = pd.Series([0.1, -0.05, 0.02, 0.0], index=idx)
        sim = wi.simulate(ret, pd.Series(1.0, index=idx), pd.Series(idx, index=idx), 1.0, NO_COST)
        assert np.allclose(sim["ret"], ret)
        assert sim["equity"].iloc[-1] == pytest.approx(np.prod(1 + ret))

    def test_tax_rate_looked_up_on_trade_date(self):
        idx = pd.bdate_range("2024-01-02", periods=1)
        seen = []
        costs = wi.CostModel(0.0, 0.0, lambda d: seen.append(d) or 0.0)
        wi.simulate(pd.Series([0.0], index=idx), pd.Series([0.0], index=idx),
                    pd.Series([pd.Timestamp("2023-12-29")], index=idx), 1.0, costs)
        assert seen == [date(2023, 12, 29)]


# ---------------------------------------------------------------- 보조 함수

class TestHelpers:
    @pytest.mark.parametrize("text,expected", [
        (None, 0), ("분할 매도 1/3 단계", 1), ("분할 매도 2/3 단계", 2), ("분할 매도 단계 (KHJ_COMBO)", 3),
    ])
    def test_stage_from_status(self, text, expected):
        assert wi.stage_from_status(text) == expected

    def test_combo_stage_configurable(self):
        assert wi.stage_from_status("분할 매도 단계 (KHJ_COMBO)", combo_stage=1) == 1

    def test_combo_value_3_forces_combo_stage(self):
        dates = pd.bdate_range("2024-01-01", periods=2)
        is_on = pd.DataFrame({"R1": [0.0, 0.0]}, index=dates)
        w = pd.DataFrame({"R1": [10.0, 10.0]}, index=dates)
        st = wi.score_and_stage(is_on, w, {"R1": "risk"}, pd.Series([2.0, 3.0], index=dates))
        assert list(st["stage"]) == [0, 3]

    def test_max_drawdown_includes_start(self):
        assert wi.max_drawdown(pd.Series([0.9, 0.95, 0.8])) == pytest.approx(-0.2)
        assert wi.max_drawdown(pd.Series([1.1, 1.2])) == 0.0

    def test_percentile_of(self):
        dist = pd.Series([0.0] * 8 + [-0.1, 0.1])
        assert wi.percentile_of(0.2, dist) == 100.0
        assert wi.percentile_of(-0.2, dist) == 0.0
        assert wi.percentile_of(0.0, dist) == pytest.approx(50.0)
        assert np.isnan(wi.percentile_of(0.0, pd.Series(dtype=float)))

    def test_rolling_windows_respect_eligibility(self):
        idx = pd.bdate_range("2024-01-01", periods=12)
        ret = pd.Series(0.01, index=idx)
        dd = pd.Series(idx, index=idx).shift(1)
        target = pd.Series(1.0, index=idx)
        target.iloc[5] = 0.0
        eligible = pd.Series(True, index=idx)
        eligible.iloc[:2] = False
        out = wi.rolling_window_diffs(ret, target, dd, eligible, 4, NO_COST)
        assert len(out) == 12 - 4 + 1 - 2
        assert out["start"].min() == idx[2]
        hit = out[(out["start"] <= idx[5]) & (out["end"] >= idx[5])]
        assert (hit["diff"] < 0).all()  # 상승장에서 하루 현금 → B보다 낮음
        assert (out.drop(hit.index)["diff"].abs() < 1e-12).all()


# ---------------------------------------------------------------- CLI 배선

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def test_cli_runs_end_to_end_on_synthetic_db(tmp_db, capsys, monkeypatch):
    idx = pd.bdate_range("2024-01-01", periods=80)
    store.upsert_prices([
        {"ticker": "000660", "date": d.strftime("%Y-%m-%d"), "open": 1, "high": 1, "low": 1,
         "close": 100 + i, "volume": 1, "source": "test"} for i, d in enumerate(idx)
    ])
    store.upsert_signal({"signal_id": "KHJ_OVERHEAT", "expert_id": "E1", "name": "t", "kind": "test", "direction": "risk",
                         "description": "", "data_source": "", "enabled": 1, "weight_cap_pct": 30.0})
    store.upsert_signal_values([
        {"date": d.strftime("%Y-%m-%d"), "signal_id": "KHJ_OVERHEAT", "value": 1.0, "is_on": 1}
        for d in idx
    ])
    store.insert_weight_history([{"recomputed_on": idx[0].strftime("%Y-%m-%d"), "signal_id": "KHJ_OVERHEAT",
                                  "old_weight_pct": None, "new_weight_pct": 30.0, "reason": "t",
                                  "source": "backfill"}])
    monkeypatch.setattr(sys, "argv", ["what_if_recent.py", "--start", idx[-10].strftime("%Y-%m-%d"),
                                      "--context-start", idx[0].strftime("%Y-%m-%d"), "--method", "weighted"])
    with pytest.raises(SystemExit) as e:
        script.main()
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "표본이 1개라 모델 성능 판단 근거가 아니다" in out
    assert "백분위" in out
    for banned in ("오를 것", "내릴 것", "매수하세요", "매도하세요"):
        assert banned not in out


# ---------------------------------------------------------------- 2026-10-18 추가: 분리 방식·맥락 분포

class TestSeparatedScoring:
    def test_opportunity_does_not_lower_risk_stage_but_legacy_does(self):
        dates = pd.bdate_range("2024-01-01", periods=1)
        is_on = pd.DataFrame({"R": [1.0], "O": [1.0]}, index=dates)
        w = pd.DataFrame({"R": [30.0], "O": [30.0]}, index=dates)
        d = {"R": "risk", "O": "opportunity"}
        sep = wi.score_and_stage(is_on, w, d)
        legacy = wi.score_and_stage(is_on, w, d, net_opportunity=True)
        assert sep["risk_score"].iloc[0] == 65.0 and sep["stage"].iloc[0] == 1
        assert legacy["risk_score"].iloc[0] == 50.0 and legacy["stage"].iloc[0] == 0
        assert sep["opportunity_score"].iloc[0] == legacy["opportunity_score"].iloc[0] == 65.0


class TestEvidence:
    @pytest.mark.parametrize("reason,expected", [
        ("backfill as_of=2020-01-31 n_close=10 evidence_signals=2/14 min_events=30", True),
        ("backfill as_of=2020-01-31 n_close=10 evidence_signals=0/14 min_events=30", False),
        ("in_sample n_events=80 hit_rate=52.5% base_rate=47.1% ci_lower=41.2% q=0.545", False),
        ("in_sample n_events=80 hit_rate=60.0% base_rate=47.1% ci_lower=50.2% q=0.01", True),
        ("in_sample n_events=12 hit_rate=70.0% base_rate=47.1% ci_lower=50.2% q=0.01", False),
        ("20d hit_rate=55.9% n=1253", None), ("no signal history yet", None), (None, None),
    ])
    def test_reason_evidence(self, reason, expected):
        assert wi.reason_evidence(reason) is expected

    def test_snapshot_evidence_uses_as_of_snapshot_and_version_rule(self):
        wh = pd.DataFrame([
            {"id": 1, "recomputed_on": "2024-01-31", "signal_id": "A", "new_weight_pct": 1.0,
             "reason": "evidence_signals=1/2", "algorithm_version": 2},
            {"id": 2, "recomputed_on": "2024-01-31", "signal_id": "A", "new_weight_pct": 1.0,
             "reason": "evidence_signals=0/2", "algorithm_version": 3},  # 큰 버전이 이긴다
            {"id": 3, "recomputed_on": "2024-02-29", "signal_id": "A", "new_weight_pct": 1.0,
             "reason": "evidence_signals=1/2", "algorithm_version": 3},
        ])
        dates = pd.DatetimeIndex(["2024-01-15", "2024-02-01", "2024-02-29", "2024-03-05"])
        assert list(wi.snapshot_evidence(wh, dates)) == [None, False, True, True]


class TestDistribution:
    def test_summary_and_independent_count(self):
        s = wi.summarize_distribution("x", pd.Series([0.0] * 6 + [-0.02, 0.03]), 0.05, window=2)
        assert (s.n, s.n_independent) == (8, 4)
        assert s.max == pytest.approx(0.03) and s.percentile == 100.0

    def test_empty(self):
        s = wi.summarize_distribution("x", pd.Series(dtype=float), 0.0, window=8)
        assert s.n == 0 and np.isnan(s.percentile)


def test_cli_prints_all_four_context_distributions_and_legacy_flag(tmp_db, capsys, monkeypatch):
    idx = pd.bdate_range("2024-01-01", periods=80)
    store.upsert_prices([
        {"ticker": "000660", "date": d.strftime("%Y-%m-%d"), "open": 1, "high": 1, "low": 1,
         "close": 100 + (i % 7) - 3 * (i % 3), "volume": 1, "source": "test"} for i, d in enumerate(idx)
    ])
    store.upsert_signal({"signal_id": "KHJ_OVERHEAT", "expert_id": "E1", "name": "t", "kind": "test",
                         "direction": "risk", "description": "", "enabled": 1})
    store.upsert_signal_values([{"date": d.strftime("%Y-%m-%d"), "signal_id": "KHJ_OVERHEAT", "value": 1.0,
                                 "is_on": int(i % 5 == 0)} for i, d in enumerate(idx)])
    store.insert_weight_history([{"recomputed_on": idx[0].strftime("%Y-%m-%d"), "signal_id": "KHJ_OVERHEAT",
                                  "old_weight_pct": None, "new_weight_pct": 40.0,
                                  "reason": "backfill evidence_signals=1/1", "source": "backfill"}])
    for extra in ([], ["--legacy-netting"]):
        monkeypatch.setattr(sys, "argv", ["what_if_recent.py", "--start", idx[-10].strftime("%Y-%m-%d"),
                                          "--context-start", idx[0].strftime("%Y-%m-%d"), "--method", "weighted",
                                          *extra])
        with pytest.raises(SystemExit) as e:
            script.main()
        assert e.value.code == 0
        out = capsys.readouterr().out
        for label in ("전체 (A−B=0 포함)", "A−B≠0 구간만", "데이터 근거 가중치 구간만", "데이터 근거 & A−B≠0"):
            assert label in out
        assert ("예전" in out) == bool(extra)


# ---------------------------------------------------------------- 2026-10-19: 그룹 방식이 기본값

def _seed_group_db(idx, on_fn):
    store.upsert_prices([{"ticker": t, "date": d.strftime("%Y-%m-%d"), "open": 100 + i % 5, "high": 1, "low": 1,
                          "close": 100 + i % 7, "volume": 1, "source": "test"}
                         for t in ("000660", "KOSPI") for i, d in enumerate(idx)])
    for sid in ("KHJ_OVERHEAT", "YJH_US10Y"):
        store.upsert_signal({"signal_id": sid, "expert_id": "E1", "name": "t", "kind": "test", "direction": "risk",
                             "description": "", "enabled": 1})
        store.upsert_signal_values([{"date": d.strftime("%Y-%m-%d"), "signal_id": sid, "value": 1.0,
                                     "is_on": on_fn(sid, i)} for i, d in enumerate(idx)])


def test_cli_group_method_is_default_and_prints_thresholds(tmp_db, capsys, monkeypatch):
    idx = pd.bdate_range("2004-06-01", "2020-03-31")
    _seed_group_db(idx, lambda sid, i: int((i % (7 if sid == "KHJ_OVERHEAT" else 11)) == 0))
    monkeypatch.setattr(sys, "argv", ["what_if_recent.py", "--start", idx[-10].strftime("%Y-%m-%d"),
                                      "--context-start", "2019-01-01"])
    with pytest.raises(SystemExit) as e:
        script.main()
    out = capsys.readouterr().out
    assert e.value.code == 0
    assert "신호 그룹 균등 가중치(기본" in out and "기준점(1/2/3단계)" in out and "2005-01-01~2018-12-31" in out
    assert "데이터 근거 가중치 구간만" not in out  # 가중치 근거 분포는 weighted 전용


def test_cli_group_method_without_calibration_data_stops_cleanly(tmp_db, capsys, monkeypatch):
    idx = pd.bdate_range("2024-01-01", periods=80)
    _seed_group_db(idx, lambda sid, i: 0)
    monkeypatch.setattr(sys, "argv", ["what_if_recent.py", "--start", idx[-10].strftime("%Y-%m-%d")])
    with pytest.raises(SystemExit) as e:
        script.main()
    assert e.value.code == 1 and "기준 기간" in capsys.readouterr().out
