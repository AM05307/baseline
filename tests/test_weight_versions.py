"""
weight_history.algorithm_version (2026-10-18) — 같은 날짜·같은 신호에 행이 여러 개일 때의 선택 규칙과
기존 DB 마이그레이션, 버전이 바뀐 백필의 append-only·멱등 동작.

규칙: algorithm_version이 큰 행 → 그다음 id가 큰 행. 행은 지우거나 고치지 않는다.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import risk_score_history as rsh  # noqa: E402
from analysis import what_if as wi  # noqa: E402
from db import store  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _legacy_db(path: Path) -> None:
    """algorithm_version 컬럼이 없던 시절의 실DB 모양(10/6 live 구·신 공식 2묶음 + 백필)."""
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE weight_history (id INTEGER PRIMARY KEY AUTOINCREMENT, recomputed_on TEXT NOT NULL,
                 signal_id TEXT NOT NULL, old_weight_pct REAL, new_weight_pct REAL NOT NULL, reason TEXT,
                 source TEXT NOT NULL DEFAULT 'live')""")
    rows = [
        ("2026-09-30", "A", None, 7.0, "backfill as_of=2026-09-30 evidence_signals=0/2", "backfill"),
        ("2026-10-06", "A", None, 30.0, "20d hit_rate=55.9% n=1253", "live"),
        ("2026-10-06", "B", None, 0.0, "no signal history yet", "live"),
        ("2026-10-06", "A", 30.0, 7.5, "in_sample n_events=80 hit_rate=52.5% base_rate=47.1% ci_lower=41.2% q=0.5", "live"),
        ("2026-10-06", "B", 0.0, 7.5, "no signal history yet", "live"),
    ]
    c.executemany("INSERT INTO weight_history (recomputed_on, signal_id, old_weight_pct, new_weight_pct, reason, source)"
                  " VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


class TestMigration:
    def test_existing_rows_are_classified_without_touching_weights(self, tmp_path):
        path = tmp_path / "legacy.db"
        _legacy_db(path)
        store.init_db(path)
        store.init_db(path)  # 멱등
        wh = store.get_weight_history(path=path).sort_values("id")
        assert list(wh["algorithm_version"]) == [2, 1, 1, 2, 2]
        assert list(wh["new_weight_pct"]) == [7.0, 30.0, 0.0, 7.5, 7.5]  # 값은 그대로
        assert store.get_current_weights(path=path) == {"A": 7.5, "B": 7.5}

    def test_unique_index_now_includes_version(self, tmp_db):
        idx = [r[1] for r in sqlite3.connect(tmp_db).execute("PRAGMA index_list(weight_history)")]
        assert "uq_weight_history_backfill_v" in idx and "uq_weight_history_backfill" not in idx


class TestSelectionRule:
    def test_higher_version_wins_even_if_inserted_earlier(self, tmp_db):
        base = {"recomputed_on": "2026-10-06", "signal_id": "A", "old_weight_pct": None, "reason": "r"}
        store.insert_weight_history([{**base, "new_weight_pct": 3.0, "algorithm_version": 3}], path=tmp_db)
        store.insert_weight_history([{**base, "new_weight_pct": 2.0, "algorithm_version": 2}], path=tmp_db)
        assert store.get_current_weights(path=tmp_db) == {"A": 3.0}
        wh = store.get_weight_history(path=tmp_db)
        assert rsh.select_weight_rows(wh)["new_weight_pct"].tolist() == [3.0]
        dates = pd.DatetimeIndex(["2026-10-07"])
        assert wi.weights_for_dates(wh, dates, ["A"]).iloc[0, 0] == 3.0
        wh2 = wh.assign(recomputed_on=pd.to_datetime(wh["recomputed_on"]))
        assert rsh.weights_as_of(wh2, dates, ["A"]).iloc[0, 0] == 3.0

    def test_same_version_later_id_wins(self, tmp_db):
        base = {"recomputed_on": "2026-10-06", "signal_id": "A", "old_weight_pct": None, "reason": "r"}
        store.insert_weight_history([{**base, "new_weight_pct": 1.0}, {**base, "new_weight_pct": 2.0}], path=tmp_db)
        assert store.get_current_weights(path=tmp_db) == {"A": 2.0}

    def test_default_version_is_current(self, tmp_db):
        store.insert_weight_history([{"recomputed_on": "2026-10-06", "signal_id": "A", "old_weight_pct": None,
                                      "new_weight_pct": 1.0, "reason": "r"}], path=tmp_db)
        assert store.get_weight_history(path=tmp_db)["algorithm_version"].tolist() == [store.CURRENT_WEIGHT_ALGORITHM_VERSION]


class TestBackfillAcrossVersions:
    def test_new_version_backfill_appends_and_is_idempotent(self, tmp_db):
        bf = {"recomputed_on": "2020-01-31", "signal_id": "A", "old_weight_pct": None,
              "reason": "r", "source": "backfill"}
        assert store.insert_weight_history([{**bf, "new_weight_pct": 5.0, "algorithm_version": 2}], path=tmp_db) == 1
        assert store.insert_weight_history([{**bf, "new_weight_pct": 6.0, "algorithm_version": 3}], path=tmp_db) == 1
        assert store.insert_weight_history([{**bf, "new_weight_pct": 9.0, "algorithm_version": 3}], path=tmp_db) == 0
        wh = store.get_weight_history(path=tmp_db)
        assert sorted(wh["new_weight_pct"]) == [5.0, 6.0]  # 옛 버전 행 보존, 새 버전 추가
        assert store.get_current_weights(path=tmp_db) == {"A": 6.0}
