"""
scripts/risk_score_history_report.py 테스트. analysis/risk_score_history.py의
순수 함수는 tests/test_risk_score_history.py에서 이미 검증했으므로, 여기서는
DB -> 그 함수들이 받는 입력 모양으로 바꾸는 배선(build_is_on_matrix)과
CLI가 에러 없이 끝까지 도는지만 확인한다(합성 데이터, 실제 수집 없음).
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from scripts import risk_score_history_report as script  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _seed_prices(ticker, tmp_db, days=300, base=100.0):
    idx = pd.bdate_range("2025-01-01", periods=days)
    rows = [
        {"ticker": ticker, "date": d.strftime("%Y-%m-%d"), "open": base, "high": base,
         "low": base, "close": base + i * 0.1, "volume": 1000, "source": "test"}
        for i, d in enumerate(idx)
    ]
    store.upsert_prices(rows, path=tmp_db)
    return idx


class TestBuildIsOnMatrix:
    def test_fills_zero_when_signal_has_no_values(self, tmp_db):
        idx = pd.bdate_range("2025-01-01", periods=5)
        out = script.build_is_on_matrix(["NEVER_COMPUTED"], idx)
        assert (out["NEVER_COMPUTED"] == 0).all()

    def test_missing_source_dates_hold_previous_value_not_off(self, tmp_db):
        """
        2026-10-18 변경: 예전엔 원천 날짜가 없는 날(예: 미국 휴장일)을 0(꺼짐)으로 채워서
        [0,1,0,1,0]이었다. 이제는 직전 값을 유지한다(analysis/signal_alignment.py).
        """
        idx = pd.bdate_range("2025-01-01", periods=5)
        store.upsert_signal_values([
            {"date": idx[1].strftime("%Y-%m-%d"), "signal_id": "A", "value": 1.0, "is_on": 1},
            {"date": idx[3].strftime("%Y-%m-%d"), "signal_id": "A", "value": 0.0, "is_on": 0},
        ], path=tmp_db)
        out = script.build_is_on_matrix(["A"], idx)
        assert list(out["A"]) == [0, 1, 1, 0, 0]

    def test_prior_session_signal_does_not_use_same_day_value(self, tmp_db):
        idx = pd.bdate_range("2025-01-01", periods=4)
        rows = [{"date": idx[2].strftime("%Y-%m-%d"), "value": 5.0, "is_on": 1}]
        store.upsert_signal_values([{**r, "signal_id": "YJH_US10Y"} for r in rows], path=tmp_db)
        store.upsert_signal_values([{**r, "signal_id": "KHJ_OVERHEAT"} for r in rows], path=tmp_db)
        out = script.build_is_on_matrix(["YJH_US10Y", "KHJ_OVERHEAT"], idx)
        assert list(out["KHJ_OVERHEAT"]) == [0, 0, 1, 1]  # 한국 장 신호: 같은 날부터
        assert list(out["YJH_US10Y"]) == [0, 0, 0, 1]  # 미국 신호: 다음 결정일부터


class TestMainCli:
    def test_runs_without_crashing_with_signals_and_weights(self, tmp_db, monkeypatch, capsys):
        idx = _seed_prices("000660", tmp_db, days=200)
        store.upsert_expert({"expert_id": "T", "name": "테스트"}, path=tmp_db)
        store.upsert_signal({
            "signal_id": "SIG_A", "name": "테스트 신호", "expert_id": "T",
            "kind": "regime_filter", "direction": "risk", "description": "test",
        }, path=tmp_db)
        store.upsert_signal_values([
            {"date": d.strftime("%Y-%m-%d"), "signal_id": "SIG_A", "value": 1.0, "is_on": int(i % 3 == 0)}
            for i, d in enumerate(idx)
        ], path=tmp_db)
        store.insert_weight_history([
            {"recomputed_on": idx[50].strftime("%Y-%m-%d"), "signal_id": "SIG_A",
             "old_weight_pct": None, "new_weight_pct": 20.0, "reason": "test"},
        ], path=tmp_db)

        monkeypatch.setattr(sys, "argv", ["risk_score_history_report.py", "--ticker", "000660"])
        sys.modules.pop("scripts.risk_score_history_report", None)
        runpy.run_module("scripts.risk_score_history_report", run_name="__main__")

        out = capsys.readouterr().out
        assert "냉시작" in out
        for phrase in ["오를 것", "매수 시점", "매도 시점"]:
            assert phrase not in out

    def test_exits_when_no_price_data(self, tmp_db, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["risk_score_history_report.py", "--ticker", "999999"])
        sys.modules.pop("scripts.risk_score_history_report", None)
        with pytest.raises(SystemExit):
            runpy.run_module("scripts.risk_score_history_report", run_name="__main__")

    def test_exits_when_no_directional_signals(self, tmp_db, monkeypatch):
        _seed_prices("000660", tmp_db, days=200)
        monkeypatch.setattr(sys, "argv", ["risk_score_history_report.py", "--ticker", "000660"])
        sys.modules.pop("scripts.risk_score_history_report", None)
        with pytest.raises(SystemExit):
            runpy.run_module("scripts.risk_score_history_report", run_name="__main__")
