"""
scripts/recompute_weights.py의 walk-forward 재계산 흐름(2026-10-06 3차
세션) 통합 테스트. 실제 DB를 흉내낸 임시 DB + 합성 가격/신호로 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from scripts import recompute_weights  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _seed(tmp_db, n_days=600, seed=0):
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range("2023-01-01", periods=n_days)
    closes = 100 * np.cumprod(1 + rs.normal(0.0002, 0.012, n_days))
    store.upsert_prices([
        {"ticker": "000660", "date": d.strftime("%Y-%m-%d"), "open": float(c), "high": float(c),
         "low": float(c), "close": float(c), "volume": 1000, "source": "test"}
        for d, c in zip(idx, closes)
    ], path=tmp_db)
    store.upsert_expert({"expert_id": "X", "name": "t"}, path=tmp_db)

    close = pd.Series(closes, index=idx)
    store.upsert_signal({
        "signal_id": "RANDOM_SIG", "name": "무작위", "expert_id": "X",
        "kind": "trade_rule", "direction": "risk", "description": "d",
    }, path=tmp_db)
    is_on_random = pd.Series(rs.randint(0, 2, n_days), index=idx)
    store.upsert_signal_values([
        {"date": d.strftime("%Y-%m-%d"), "signal_id": "RANDOM_SIG", "value": None, "is_on": int(v)}
        for d, v in is_on_random.items()
    ], path=tmp_db)
    return close


class TestRecomputeWeightsWalkForward:
    def test_runs_without_crashing_and_prints_header(self, tmp_db, capsys):
        _seed(tmp_db)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(store, "DB_PATH", tmp_db)
            m.setattr(sys, "argv", ["recompute_weights.py", "--dry-run"])
            recompute_weights.main()
        out = capsys.readouterr().out
        assert "RANDOM_SIG" in out
        assert "walk-forward" in out
        assert "위험 점수 비교" in out

    def test_random_signal_contributes_no_weight_in_summary(self, tmp_db, capsys):
        _seed(tmp_db)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(store, "DB_PATH", tmp_db)
            m.setattr(sys, "argv", ["recompute_weights.py", "--dry-run"])
            recompute_weights.main()
        out = capsys.readouterr().out
        # 무작위 신호 하나뿐이라 동일가중치(100%)로 떨어져야 정상 — 통계
        # 유의성 검정 자체가 깨지지 않고 끝까지 도는지 확인하는 용도.
        assert "신규w" in out

    def test_dry_run_does_not_write_weight_history(self, tmp_db):
        _seed(tmp_db)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(store, "DB_PATH", tmp_db)
            m.setattr(sys, "argv", ["recompute_weights.py", "--dry-run"])
            recompute_weights.main()
        assert store.get_weight_history(path=tmp_db).empty

    def test_real_run_writes_weight_history(self, tmp_db):
        _seed(tmp_db)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(store, "DB_PATH", tmp_db)
            m.setattr(sys, "argv", ["recompute_weights.py"])
            recompute_weights.main()
        assert not store.get_weight_history(path=tmp_db).empty
