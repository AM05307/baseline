"""
collectors/marks_memos.py — 가짜 HTML(tests/fixtures)로 파싱·확인 단계·멱등 저장을 검증한다(네트워크 없음).
실제 오크트리 사이트 호출은 MARKS_LIVE_TEST=1일 때만.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors import marks_memos as mm  # noqa: E402
from db import store  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
LIST_HTML = (FIX / "marks_memos_list.html").read_text()
PAGE_HTML = (FIX / "marks_memo_page.html").read_text()
BASE = "https://www.oaktreecapital.com/insights/memos"
CFG = {"list_url": BASE, "robots_url": "https://www.oaktreecapital.com/robots.txt", "terms_url": "https://x/terms",
       "terms_reviewed": True, "user_agent": "baseline-test", "request_interval_seconds": 0}
ALLOW = "User-agent: *\nAllow: /\n"


def _fetch(robots=ALLOW, calls=None):
    def f(url, ua):
        if calls is not None:
            calls.append(url)
        if url.endswith("robots.txt"):
            return robots
        if url == BASE:
            return LIST_HTML
        return PAGE_HTML
    return f


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init_db(p)
    return p


class TestParse:
    def test_memo_links_titles_and_dates(self):
        items = {m["url"].rsplit("/", 1)[-1]: m for m in mm.parse_memo_list(LIST_HTML, BASE)}
        assert set(items) == {"fake-memo-alpha", "fake-memo-beta", "fake-memo-gamma"}  # 목록 링크·팟캐스트·스크립트 제외
        assert items["fake-memo-alpha"]["title"] == "Fake Memo Alpha"  # 제목 없는 이미지 링크와 합쳐짐
        assert items["fake-memo-alpha"]["published_on"] == "2026-09-15"
        assert items["fake-memo-beta"]["published_on"] == "2026-07-02" and items["fake-memo-beta"]["title"] == "Fake Memo Beta"
        assert items["fake-memo-beta"]["url"].endswith("fake-memo-beta")  # #fragment 제거
        assert items["fake-memo-gamma"]["published_on"] is None  # 날짜를 지어내지 않음

    def test_memo_text_drops_chrome_and_scripts(self):
        t = mm.memo_text(PAGE_HTML)
        assert "Placeholder paragraph one" in t and "tracking" not in t and "Site header" not in t


class TestPermission:
    def test_terms_not_reviewed_blocks_and_records_manual_required(self, db):
        calls = []
        r = mm.collect({**CFG, "terms_reviewed": False}, _fetch(calls=calls), sleep=lambda s: None, path=db)
        assert r["status"] == "manual_required" and "이용약관" in r["reason"]
        assert calls == []  # 약관 확인 전에는 robots.txt조차 받지 않는다
        log = store.recent_logs(path=db)
        assert log.iloc[0]["status"] == "manual_required" and store.get_memos(db).empty

    def test_robots_disallow_blocks(self, db):
        r = mm.collect(CFG, _fetch(robots="User-agent: *\nDisallow: /insights/\n"), sleep=lambda s: None, path=db)
        assert r["status"] == "manual_required" and "robots.txt" in r["reason"] and store.get_memos(db).empty

    def test_robots_fetch_failure_blocks(self, db):
        def boom(url, ua):
            raise ConnectionError("x")
        assert mm.collect(CFG, boom, sleep=lambda s: None, path=db)["status"] == "manual_required"


class TestCollect:
    def test_collect_stores_metadata_and_hash_only(self, db):
        r = mm.collect(CFG, _fetch(), sleep=lambda s: None, path=db)
        assert r == {"status": "ok", "reason": "", "new": 3, "seen": 3}
        memos = store.get_memos(db)
        assert set(memos.columns) >= {"url", "title", "published_on", "body_hash", "collected_on"}
        assert memos["body_hash"].notna().all()
        dump = "\n".join(str(r) for r in sqlite3.connect(db).execute("SELECT * FROM memos").fetchall())
        assert "Placeholder paragraph" not in dump  # 본문은 DB 어디에도 없다

    def test_rerun_is_idempotent_and_skips_known_pages(self, db):
        mm.collect(CFG, _fetch(), sleep=lambda s: None, path=db)
        calls = []
        r = mm.collect(CFG, _fetch(calls=calls), sleep=lambda s: None, path=db)
        assert r["new"] == 0 and len(store.get_memos(db)) == 3
        assert all("fake-memo" not in c for c in calls)  # 이미 아는 메모 페이지는 다시 안 받음

    def test_manual_entry_and_own_summary(self, db):
        assert mm.add_manual("Fake Manual Memo", "https://x/insights/memo/m", "2026-10-01", "내가 쓴 한 줄 요약", path=db)
        assert not mm.add_manual("Fake Manual Memo", "https://x/insights/memo/m", None, path=db)  # 멱등
        row = store.latest_memo(db)
        assert row["source"] == "manual" and row["summary_by"] == "manual" and row["published_on"] == "2026-10-01"

    def test_fetch_memo_text_respects_permission(self):
        assert mm.fetch_memo_text("https://www.oaktreecapital.com/insights/memo/a", {**CFG, "terms_reviewed": False},
                                  _fetch()) is None
        assert "Placeholder" in mm.fetch_memo_text("https://www.oaktreecapital.com/insights/memo/a", CFG, _fetch())

    def test_shipped_config_is_not_reviewed(self):
        assert mm.load_config()["terms_reviewed"] is False


@pytest.mark.skipif(os.environ.get("MARKS_LIVE_TEST") != "1", reason="실제 네트워크는 MARKS_LIVE_TEST=1일 때만")
def test_live_permission_check_only():
    """실제 robots.txt만 확인한다(목록 수집은 약관 확인 표시가 있어야 함)."""
    perm = mm.check_permission({**mm.load_config(), "terms_reviewed": True}, mm.default_fetch)
    assert isinstance(perm.allowed, bool)
