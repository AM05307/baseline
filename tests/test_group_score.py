"""신호 그룹 균등 가중치 + 백분위 단계(analysis/group_score) — 2026-10-19 기본 방식."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import group_score as gs  # noqa: E402
from analysis import what_if as wi  # noqa: E402

G = {"A": ("a1", "a2"), "B": ("b1",), "C": ("c1",)}


def _frame(rows, cols=("a1", "a2", "b1", "c1"), start="2024-01-01"):
    return pd.DataFrame(rows, columns=list(cols), index=pd.bdate_range(start, periods=len(rows)), dtype=float)


class TestScore:
    def test_equal_weights_within_and_across_groups(self):
        df = _frame([[1, 0, 1, 0]])
        # A = 0.5, B = 1, C = 0 → 평균 0.5 → 50
        assert gs.risk_score_series(df, groups=G).iloc[0] == pytest.approx(50.0)

    def test_missing_values_leave_the_denominator(self):
        df = _frame([[1, np.nan, np.nan, 0]])
        # A = 1/1, B = 값 없음(제외), C = 0 → (1 + 0)/2 = 50
        assert gs.risk_score_series(df, groups=G).iloc[0] == pytest.approx(50.0)
        assert gs.coverage(df, groups=G).iloc[0] == 2

    def test_exclude_removes_signal_and_empty_group(self):
        df = _frame([[1, 1, 1, 1]])
        assert gs.risk_score_series(df, exclude={"b1"}, groups=G).iloc[0] == pytest.approx(100.0)
        assert gs.coverage(df, exclude={"b1"}, groups=G).iloc[0] == 2

    def test_no_groups_with_data_is_nan_not_zero(self):
        df = _frame([[np.nan] * 4])
        assert np.isnan(gs.risk_score_series(df, groups=G).iloc[0])

    def test_default_groups_match_design_6_2(self):
        assert gs.RISK_GROUPS["R1 미국 금리"] == ("YJH_US10Y", "KSJ_US10Y_HIGH", "KHJ_RATE_STAGE")
        assert set(gs.RISK_GROUPS["R2 글로벌 위험선호"]) == {"KHJ_CONCENTRATION", "KHJ_IPO_WEAK", "KHJ_COMBO"}
        assert set(gs.RISK_GROUPS["R3 000660 가격 과열"]) == {"KHJ_OVERHEAT", "YJH_BOX_RANGE_TRIM"}
        assert (gs.CALIBRATION_START, gs.CALIBRATION_END, gs.VALIDATION_START) == ("2005-01-01", "2018-12-31", "2019-01-01")
        assert gs.STAGE_QUANTILES == (0.30, 0.15, 0.05)


class TestCalibration:
    def _scores(self, values, start="2010-01-01"):
        return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)

    def test_thresholds_respect_quantiles_with_ties(self):
        # 100일: 0점 60일, 25점 25일, 50점 10일, 100점 5일
        s = self._scores([0] * 60 + [25] * 25 + [50] * 10 + [100] * 5)
        thr = gs.calibrate_thresholds(s, "2010-01-01", "2010-12-31")
        assert thr.values == (50.0, 50.0, 100.0)  # 25점 이상은 40% > 30% → 50점(15%)이 1단계
        assert thr.actual_shares == pytest.approx((0.15, 0.15, 0.05))
        assert all(a <= q + 1e-12 for a, q in zip(thr.actual_shares, thr.quantiles))

    def test_unreachable_when_max_value_too_common(self):
        s = self._scores([0] * 50 + [100] * 50)
        thr = gs.calibrate_thresholds(s, "2010-01-01", "2010-12-31")
        assert thr.values == (float("inf"),) * 3
        assert (gs.stage_series(s, thr) == 0).all()

    def test_only_calibration_window_is_used(self):
        cal = self._scores([0] * 70 + [10] * 30, start="2010-01-01")
        later = self._scores([10] * 100, start="2011-01-03")
        both = pd.concat([cal, later])
        assert gs.calibrate_thresholds(both, "2010-01-01", "2010-12-31") == gs.calibrate_thresholds(cal, "2010-01-01", "2010-12-31")

    def test_empty_calibration_window_raises(self):
        with pytest.raises(ValueError):
            gs.calibrate_thresholds(self._scores([1, 2, 3], start="2020-01-01"), "2005-01-01", "2018-12-31")

    def test_stage_series_and_status(self):
        thr = gs.Thresholds((10.0, 20.0, 30.0), (0.3, 0.15, 0.05), (0.3, 0.15, 0.05), 100, "x", "y")
        s = self._scores([0, 10, 25, 30, np.nan])
        assert list(gs.stage_series(s, thr)) == [0, 1, 2, 3, 0]
        assert gs.stage_status(0) == ("매수 가능", None)
        assert gs.stage_status(3) == ("보류", "분할 매도 3/3 단계")
        assert wi.stage_from_status("분할 매도 3/3 단계") == 3


def test_group_states_future_values_do_not_change_past_stage():
    """기준점은 기준 기간만, 단계는 그날 값만 — 기준 기간 뒤를 바꿔도 그 전 결정은 그대로."""
    idx = pd.bdate_range("2005-01-03", "2020-12-31")
    rng = np.random.default_rng(3)
    is_on = pd.DataFrame(rng.integers(0, 2, (len(idx), 3)), index=idx, columns=["YJH_US10Y", "KHJ_OVERHEAT", "KSJ_BREADTH"],
                         dtype=float)
    base = wi.group_states(is_on, idx)
    alt = is_on.copy()
    alt.loc["2019-06-01":] = 1.0
    changed = wi.group_states(alt, idx)
    assert base.attrs["thresholds"] == changed.attrs["thresholds"]
    pd.testing.assert_series_equal(base["stage"][:"2019-05-31"], changed["stage"][:"2019-05-31"])


def test_report_script_runs_on_synthetic_db(tmp_path, monkeypatch, capsys):
    from db import store
    from scripts import group_score_report
    db = tmp_path / "b.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init_db(db)
    idx = pd.bdate_range("2004-06-01", "2020-06-30")
    store.upsert_prices([{"ticker": t, "date": d.strftime("%Y-%m-%d"), "open": 100 + i % 5, "high": 1, "low": 1,
                          "close": 100 + (i % 13) - (i % 7), "volume": 1, "source": "t"}
                         for t in ("000660", "KOSPI") for i, d in enumerate(idx)])
    for sid, m in (("KHJ_OVERHEAT", 7), ("YJH_US10Y", 11)):
        store.upsert_signal({"signal_id": sid, "expert_id": "E", "name": sid, "kind": "t", "direction": "risk",
                             "description": "", "enabled": 1})
        store.upsert_signal_values([{"date": d.strftime("%Y-%m-%d"), "signal_id": sid, "value": 1.0,
                                     "is_on": int(i % m == 0)} for i, d in enumerate(idx)])
    assert group_score_report.main([]) == 0
    out = capsys.readouterr().out
    assert "기준 기간" in out and "검증 기간" in out and "단순 보유" in out
    for banned in ("오를 것", "내릴 것", "매수하세요", "매도하세요"):
        assert banned not in out
