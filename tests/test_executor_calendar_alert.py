"""다음 해 휴장일 목록 경고 — 12/1 이후인데 config/krx_holidays.yaml이 다음 해를 덮지 않으면 섀도 로그·executor 화면에 경고."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import shadow, store, web  # noqa: E402
from executor.krx_calendar import next_year_warning, parse_calendar  # noqa: E402

CAL_2026 = parse_calendar({"covered_from": "2026-01-01", "covered_to": "2026-12-31", "holidays": []})
CAL_2027 = parse_calendar({"covered_from": "2026-01-01", "covered_to": "2027-12-31", "holidays": []})


@pytest.mark.parametrize("today, cal, warns", [
    (date(2026, 11, 30), CAL_2026, False),  # 12/1 전
    (date(2026, 12, 1), CAL_2026, True),
    (date(2026, 12, 31), CAL_2026, True),
    (date(2026, 12, 1), CAL_2027, False),  # 다음 해 목록 있음
])
def test_next_year_warning(today, cal, warns):
    w = next_year_warning(cal, today)
    assert bool(w) is warns
    if warns:
        assert "2027" in w and "krx_holidays.yaml" in w


def test_run_shadow_logs_warning_even_when_skipped(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    now = datetime(2026, 12, 4, 10, 0, tzinfo=shadow.KST)  # 장 마감 전 → 실행은 건너뜀
    out = shadow.run_shadow(now.date(), now, calendar=CAL_2026, cfg={"kill_switch_file": "KS"},
                            kill_switch_file=tmp_path / "KS", path=db)
    assert out.status == "skipped_before_close" and out.warnings
    assert "calendar_warning" in set(store.events(path=db)["kind"])


def test_executor_page_shows_warning(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "none.db")
    monkeypatch.setattr(web, "calendar_warning", lambda today=None: "KRX 휴장일 캘린더 경고 테스트")
    html = TestClient(web._make_app()).get("/").text
    assert "KRX 휴장일 캘린더 경고 테스트" in html
    monkeypatch.setattr(web, "calendar_warning", lambda today=None: None)
    assert "휴장일 캘린더</span>" not in TestClient(web._make_app()).get("/").text
