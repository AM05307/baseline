"""db/store.py의 manual_inputs(append-only 타임라인) 테스트."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


class TestInsertManualInput:
    def test_is_append_only(self, tmp_db):
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-01-01", "value_num": 1.5}, path=tmp_db)
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-02-01", "value_num": 1.2}, path=tmp_db)
        df = store.get_manual_timeline("pbr", path=tmp_db)
        assert len(df) == 2  # 덮어쓰지 않고 둘 다 남는다


class TestGetManualTimeline:
    def test_empty_when_nothing_recorded(self, tmp_db):
        assert store.get_manual_timeline("rate_stage", path=tmp_db).empty

    def test_sorted_by_effective_on(self, tmp_db):
        store.insert_manual_input({"input_type": "rate_stage", "effective_on": "2026-06-01", "value_text": "1회"}, path=tmp_db)
        store.insert_manual_input({"input_type": "rate_stage", "effective_on": "2026-01-01", "value_text": "인상 전"}, path=tmp_db)
        df = store.get_manual_timeline("rate_stage", path=tmp_db)
        assert list(df["value_text"]) == ["인상 전", "1회"]

    def test_correction_keeps_latest_entry_for_same_effective_on(self, tmp_db):
        # 같은 effective_on을 두 번 입력(정정) -> entered_at이 더 늦은 값이 유효해야 함
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-01-01", "value_num": 1.5}, path=tmp_db)
        time.sleep(1.1)  # entered_at은 초 단위라 확실히 구분되게
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-01-01", "value_num": 1.3}, path=tmp_db)
        df = store.get_manual_timeline("pbr", path=tmp_db)
        assert len(df) == 1
        assert df.iloc[0]["value_num"] == pytest.approx(1.3)

    def test_different_input_types_are_isolated(self, tmp_db):
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-01-01", "value_num": 1.5}, path=tmp_db)
        store.insert_manual_input({"input_type": "memory_margin", "effective_on": "2026-01-01", "value_num": 70.0}, path=tmp_db)
        assert len(store.get_manual_timeline("pbr", path=tmp_db)) == 1
        assert len(store.get_manual_timeline("memory_margin", path=tmp_db)) == 1


class TestManualInputFreshness:
    def test_no_input_at_all_returns_exists_false(self, tmp_db):
        out = store.manual_input_freshness("pbr", as_of="2026-06-01", path=tmp_db)
        assert out == {"input_type": "pbr", "exists": False, "effective_on": None, "age_days": None, "stale": None}

    def test_fresh_input_is_not_stale(self, tmp_db):
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-06-01", "value_num": 1.1}, path=tmp_db)
        out = store.manual_input_freshness("pbr", as_of="2026-06-10", stale_days=90, path=tmp_db)
        assert out["exists"] is True
        assert out["age_days"] == 9
        assert out["stale"] is False

    def test_old_input_is_stale(self, tmp_db):
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-01-01", "value_num": 1.1}, path=tmp_db)
        out = store.manual_input_freshness("pbr", as_of="2026-06-01", stale_days=90, path=tmp_db)
        assert out["age_days"] > 90
        assert out["stale"] is True

    def test_uses_most_recent_value_as_of_date_not_future_entries(self, tmp_db):
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-01-01", "value_num": 1.1}, path=tmp_db)
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-12-01", "value_num": 2.2}, path=tmp_db)
        # as_of가 2026-06-01이면 2026-12-01 입력은 아직 "미래"라 무시해야 함 (미래 참조 금지)
        out = store.manual_input_freshness("pbr", as_of="2026-06-01", path=tmp_db)
        assert out["effective_on"] == "2026-01-01"

    def test_no_value_valid_as_of_given_date_returns_exists_false(self, tmp_db):
        store.insert_manual_input({"input_type": "pbr", "effective_on": "2026-12-01", "value_num": 2.2}, path=tmp_db)
        out = store.manual_input_freshness("pbr", as_of="2026-01-01", path=tmp_db)
        assert out["exists"] is False

    def test_defaults_as_of_to_today_when_omitted(self, tmp_db):
        today = pd.Timestamp.now().normalize().date().isoformat()
        store.insert_manual_input({"input_type": "pbr", "effective_on": today, "value_num": 1.1}, path=tmp_db)
        out = store.manual_input_freshness("pbr", path=tmp_db)
        assert out["exists"] is True
        assert out["age_days"] == 0
