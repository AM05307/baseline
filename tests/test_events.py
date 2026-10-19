"""analysis/events.py · events 테이블 · scripts/add_event.py · 대시보드 연결 테스트."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import events as ev  # noqa: E402
from db import store  # noqa: E402
from scripts import add_event  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


class TestNormalizeAndValidate:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-01-05", "2026-01-05"), ("20260105", "2026-01-05"), ("2026.01.05", "2026-01-05"),
        ("2026년 01월 05일", "2026-01-05"), (date(2026, 1, 5), "2026-01-05"),
        (pd.Timestamp("2026-01-05"), "2026-01-05"),
    ])
    def test_normalize_date_formats(self, raw, expected):
        assert ev.normalize_date(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "abc", "2026-13-40", "미정"])
    def test_normalize_date_rejects_garbage(self, raw):
        assert ev.normalize_date(raw) is None

    def test_single_day_event_end_defaults_to_start(self):
        r = ev.validate_event({"ticker": "000660", "event_type": "earnings", "start_date": "2026-10-23"})
        assert r["end_date"] == "2026-10-23" and r["source"] == "manual"

    def test_buyback_requires_explicit_end(self):
        with pytest.raises(ValueError, match="end_date"):
            ev.validate_event({"ticker": "000660", "event_type": "buyback", "start_date": "2026-03-04"})
        with pytest.raises(ValueError, match="end_date"):
            ev.validate_event({"ticker": "000660", "event_type": "buyback", "start_date": "2026-03-04", "end_date": ""})

    def test_rejects_bad_type_dates_and_order(self):
        base = {"ticker": "000660", "event_type": "buyback", "start_date": "2026-03-04", "end_date": "2026-06-03"}
        assert ev.validate_event(base)["end_date"] == "2026-06-03"
        with pytest.raises(ValueError):
            ev.validate_event({**base, "event_type": "dividend"})
        with pytest.raises(ValueError):
            ev.validate_event({**base, "start_date": "not-a-date"})
        with pytest.raises(ValueError, match="빠릅니다"):
            ev.validate_event({**base, "end_date": "2026-03-01"})
        with pytest.raises(ValueError):
            ev.validate_event({**base, "ticker": " "})
        with pytest.raises(ValueError):
            ev.validate_event({**base, "source": "rumor"})


class TestDartExtraction:
    def test_is_buyback_decision(self):
        assert ev.is_buyback_decision("주요사항보고서(자기주식취득결정)")
        assert ev.is_buyback_decision("[기재정정]주요사항보고서(자기주식 취득 결정)")
        assert ev.is_buyback_decision("주요사항보고서(자기주식취득신탁계약체결결정)")
        assert not ev.is_buyback_decision("주요사항보고서(자기주식처분결정)")      # 처분 ≠ 취득
        assert not ev.is_buyback_decision("자기주식취득결과보고서")                 # 이번 범위 밖(TODO)
        assert not ev.is_buyback_decision("분기보고서 (2026.09)")
        assert not ev.is_buyback_decision(None)

    def test_parse_period_candidate_fields_and_failures(self):
        assert ev.parse_buyback_period({"aqexpd_bgd": "2026년 03월 04일", "aqexpd_edd": "2026년 06월 03일"}) == ("2026-03-04", "2026-06-03")
        assert ev.parse_buyback_period({"aq_expd_bgd": "20260304", "aq_expd_edd": "20260603"}) == ("2026-03-04", "2026-06-03")
        assert ev.parse_buyback_period({"aqexpd_bgd": "20260304"}) is None            # 종료 없음
        assert ev.parse_buyback_period({"aqexpd_bgd": "-", "aqexpd_edd": "-"}) is None
        assert ev.parse_buyback_period({"aqexpd_bgd": "20260603", "aqexpd_edd": "20260304"}) is None
        assert ev.parse_buyback_period({"unrelated": 1}) is None

    def _disclosures(self):
        return pd.DataFrame([
            {"rcept_no": "A1", "report_nm": "주요사항보고서(자기주식취득결정)", "rcept_dt": "2026-03-03", "stock_code": "000660"},
            {"rcept_no": "A2", "report_nm": "[기재정정]주요사항보고서(자기주식취득결정)", "rcept_dt": "2026-03-10", "stock_code": "000660"},
            {"rcept_no": "B1", "report_nm": "분기보고서 (2026.03)", "rcept_dt": "2026-05-15", "stock_code": "000660"},
            {"rcept_no": "C1", "report_nm": "주요사항보고서(자기주식처분결정)", "rcept_dt": "2026-04-01", "stock_code": "000660"},
        ])

    def test_without_details_nothing_is_invented(self):
        res = ev.extract_buyback_events(self._disclosures(), details=None)
        assert res.events == []
        assert [u["rcept_no"] for u in res.unresolved] == ["A1", "A2"]
        assert {u["reason"] for u in res.unresolved} == {"period_unknown"}

    def test_with_details_builds_deduped_events(self):
        period = {"aqexpd_bgd": "20260304", "aqexpd_edd": "20260603"}
        res = ev.extract_buyback_events(self._disclosures(), details={"A1": period, "A2": period})
        assert len(res.events) == 1                       # 정정공시가 같은 기간을 다시 내도 한 번만
        e = res.events[0]
        assert (e["ticker"], e["event_type"], e["start_date"], e["end_date"], e["source"], e["source_ref"]) == \
               ("000660", "buyback", "2026-03-04", "2026-06-03", "dart", "A1")

    def test_unparseable_detail_and_missing_stock_code_are_reported(self):
        df = self._disclosures().iloc[:2].copy()
        df.loc[1, "stock_code"] = None
        res = ev.extract_buyback_events(df, details={"A1": {"x": 1}, "A2": {"aqexpd_bgd": "20260304", "aqexpd_edd": "20260603"}})
        assert res.events == []
        assert {u["reason"] for u in res.unresolved} == {"period_unparseable", "stock_code_missing"}

    def test_empty_input(self):
        assert ev.extract_buyback_events(pd.DataFrame()).events == []
        assert ev.extract_buyback_events(None).unresolved == []


class TestEventsTable:
    def _row(self, **kw):
        return ev.validate_event({"ticker": "000660", "event_type": "buyback", "start_date": "2026-03-04",
                                  "end_date": "2026-06-03", **kw})

    def test_upsert_is_idempotent(self, tmp_db):
        assert store.upsert_event(self._row(), path=tmp_db) == "inserted"
        assert store.upsert_event(self._row(), path=tmp_db) == "updated"
        assert len(store.get_events(path=tmp_db)) == 1

    def test_manual_overrides_only_with_new_values_and_wins_source(self, tmp_db):
        store.upsert_event(self._row(source="dart", source_ref="A1", title="공시 제목"), path=tmp_db)
        store.upsert_event(self._row(note="직접 메모"), path=tmp_db)  # manual: title 없음 → 기존 유지
        r = store.get_events(path=tmp_db).iloc[0]
        assert (r["title"], r["note"], r["source"], r["source_ref"]) == ("공시 제목", "직접 메모", "manual", "A1")
        store.upsert_event(self._row(title="수정한 제목"), path=tmp_db)
        assert store.get_events(path=tmp_db).iloc[0]["title"] == "수정한 제목"

    def test_dart_never_overwrites_manual_values(self, tmp_db):
        store.upsert_event(self._row(title="내가 쓴 제목", note="내 메모"), path=tmp_db)
        store.upsert_event(self._row(source="dart", source_ref="A9", title="공시 제목", note="자동 메모"), path=tmp_db)
        r = store.get_events(path=tmp_db).iloc[0]
        assert (r["title"], r["note"], r["source"]) == ("내가 쓴 제목", "내 메모", "manual")
        assert r["source_ref"] == "A9"               # 비어 있던 출처 참조만 채움

    def test_filters_and_order(self, tmp_db):
        store.upsert_event(ev.validate_event({"ticker": "000660", "event_type": "earnings", "start_date": "2026-10-23"}), path=tmp_db)
        store.upsert_event(self._row(), path=tmp_db)
        store.upsert_event(ev.validate_event({"ticker": "005930", "event_type": "earnings", "start_date": "2026-10-30"}), path=tmp_db)
        assert list(store.get_events("000660", path=tmp_db)["event_type"]) == ["buyback", "earnings"]
        assert len(store.get_events(event_type="earnings", path=tmp_db)) == 2
        assert store.get_events("999999", path=tmp_db).empty

    def test_existing_db_without_events_table_is_upgraded(self, tmp_path):
        path = tmp_path / "old.db"
        c = sqlite3.connect(path)
        c.execute("CREATE TABLE daily_price (ticker TEXT, date TEXT, close REAL)")
        c.commit(); c.close()
        store.init_db(path)
        store.init_db(path)
        assert store.get_events(path=path).empty


class TestAddEventScript:
    def _run(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["add_event.py", *argv])
        add_event.main()

    def test_add_twice_is_idempotent(self, tmp_db, monkeypatch, capsys):
        args = ("--type", "buyback", "--start", "2026-03-04", "--end", "2026-06-03", "--note", "메모")
        self._run(monkeypatch, *args)
        self._run(monkeypatch, *args)
        assert len(store.get_events(path=tmp_db)) == 1
        out = capsys.readouterr().out
        assert "새로 저장" in out and "이미 있는 이벤트" in out

    def test_buyback_without_end_exits_with_error_and_saves_nothing(self, tmp_db, monkeypatch, capsys):
        with pytest.raises(SystemExit) as e:
            self._run(monkeypatch, "--type", "buyback", "--start", "2026-03-04")
        assert e.value.code == 2
        assert store.get_events(path=tmp_db).empty
        assert "end_date" in capsys.readouterr().out

    def test_dry_run_saves_nothing(self, tmp_db, monkeypatch, capsys):
        self._run(monkeypatch, "--type", "earnings", "--start", "2026-10-23", "--dry-run")
        assert store.get_events(path=tmp_db).empty
        assert "저장하지 않았습니다" in capsys.readouterr().out

    def test_list_and_empty_list(self, tmp_db, monkeypatch, capsys):
        self._run(monkeypatch, "--list")
        assert "등록된 이벤트가 없습니다" in capsys.readouterr().out
        self._run(monkeypatch, "--type", "conf_call", "--start", "2026-10-23", "--title", "3분기 콘퍼런스콜")
        self._run(monkeypatch, "--list")
        out = capsys.readouterr().out
        assert "콘퍼런스콜" in out and "2026-10-23" in out

    def test_missing_required_args_is_usage_error(self, tmp_db, monkeypatch):
        with pytest.raises(SystemExit):
            self._run(monkeypatch, "--start", "2026-10-23")

    def test_from_dart_makes_no_events_without_period_details_and_never_touches_network(self, tmp_db, monkeypatch, capsys):
        store.upsert_disclosures([{
            "rcept_no": "20260303000001", "corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": "000660",
            "report_nm": "주요사항보고서(자기주식취득결정)", "rcept_dt": "2026-03-03", "flr_nm": "SK하이닉스", "rm": "유",
        }], path=tmp_db)
        import collectors.dart_client as dc
        monkeypatch.setattr(dc.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network!")), raising=False)
        self._run(monkeypatch, "--from-dart")
        out = capsys.readouterr().out
        assert "기간 미확인" in out and "period_unknown" in out
        assert store.get_events(path=tmp_db).empty
