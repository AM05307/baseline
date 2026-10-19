"""
하워드 막스(오크트리) 공개 메모 수집기 — 주 1회면 충분하다(새 메모는 몇 주~몇 달에 한 편).

저장하는 것: 제목·게시일·URL·본문 해시·수집일(db/schema.sql memos). **본문 전체는 저장하지 않는다** — 해시만 남겨
변경을 알아채고, 요약이 필요할 때(scripts/run_debate.py --execute)만 그때 다시 받아 메모리에서 쓰고 버린다.
요약은 개인 참고용이며 외부에 공개하지 않는다(README). 원문 문장을 옮겨 적지 않는다(CLAUDE.md 원칙 5).

수집 전 확인(config/marks_memos.yaml):
  1. 이용약관 — 사람이 확인하고 terms_reviewed: true로 바꾸기 전에는 수집하지 않는다(코드가 약관을 판정할 수 없다).
  2. robots.txt — 매번 확인. 목록·메모 URL을 허용하지 않으면 수집하지 않는다.
허용되지 않으면 collect_log에 status='manual_required'와 사유를 남기고 끝낸다. 그때는 --manual로 직접 입력한다.

페이지 구조(목록의 어떤 링크가 메모인지, 날짜가 어디 있는지)는 **실제 페이지로 확인하지 못했다** — 파서는 tests/fixtures의
가짜 HTML로만 검증했다. 첫 실행 때 파싱 결과가 0건이거나 이상하면 parse_memo_list()의 규칙을 실제 구조에 맞출 것.

사용:
    python collectors/marks_memos.py                 # 확인 단계 통과 시 새 메모만 수집
    python collectors/marks_memos.py --manual --title "제목" --url https://... --date 2026-10-01 [--summary "내 요약"]
    python scripts/run_weekly.py --only memos        # 주간 체인에서 메모만
실제 네트워크 테스트는 MARKS_LIVE_TEST=1일 때만(tests/test_marks_memos.py).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "marks_memos.yaml"
JOB = "marks_memos"
MEMO_HREF = re.compile(r"/insights/memos?/[^/?#]+", re.IGNORECASE)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
TEXT_DATE = re.compile(rf"\b({MONTHS}) (\d{{1,2}}), (\d{{4}})\b")

Fetch = Callable[[str, str], str]  # (url, user_agent) -> 본문 텍스트(HTML). 실패는 예외.


def load_config(path: Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def default_fetch(url: str, user_agent: str) -> str:
    import requests
    r = requests.get(url, headers={"User-Agent": user_agent}, timeout=20)
    r.raise_for_status()
    return r.text


@dataclass
class Permission:
    allowed: bool
    reason: str
    robots: RobotFileParser | None = None


def check_permission(cfg: dict, fetch: Fetch) -> Permission:
    """이용약관(사람 확인 표시) → robots.txt(코드 확인) 순서. 하나라도 아니면 수집하지 않는다."""
    if not cfg.get("terms_reviewed"):
        return Permission(False, f"수동 입력 필요: 이용약관 미확인 — {cfg.get('terms_url')}을 읽고 "
                                 "config/marks_memos.yaml의 terms_reviewed를 true로 바꾸기 전에는 자동 수집하지 않음")
    ua = cfg.get("user_agent", "baseline")
    try:
        robots_txt = fetch(cfg["robots_url"], ua)
    except Exception as e:
        return Permission(False, f"수동 입력 필요: robots.txt 확인 실패({type(e).__name__}) — 확인 못 하면 수집하지 않음")
    rp = RobotFileParser()
    rp.parse(robots_txt.splitlines())
    if not rp.can_fetch(ua, cfg["list_url"]):
        return Permission(False, "수동 입력 필요: robots.txt가 메모 목록 페이지 수집을 허용하지 않음")
    return Permission(True, "허용", rp)


class _ListParser(HTMLParser):
    """메모 링크(<a href=…/insights/memo/…>)와, 그 뒤 다음 메모 링크 전까지 처음 나오는 날짜를 짝짓는다."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base = base_url
        self.items: list[dict] = []
        self._in_anchor: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href") and MEMO_HREF.search(a["href"]):
            url = urljoin(self.base, a["href"]).split("#")[0]
            if url.rstrip("/") == self.base.rstrip("/"):
                return
            self._in_anchor = {"url": url, "title": "", "published_on": None}
            self.items.append(self._in_anchor)
        elif tag == "time" and a.get("datetime") and self.items and self.items[-1]["published_on"] is None:
            self.items[-1]["published_on"] = _iso(a["datetime"][:10])

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_anchor = None

    def handle_data(self, data):
        if self._in_anchor is not None:
            self._in_anchor["title"] += data
        elif self.items and self.items[-1]["published_on"] is None:
            m = TEXT_DATE.search(data)
            if m:
                self.items[-1]["published_on"] = datetime.strptime(m.group(0), "%B %d, %Y").date().isoformat()


def _iso(s: str) -> str | None:
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def parse_memo_list(html: str, base_url: str) -> list[dict]:
    """[{url, title, published_on}] — 같은 URL은 한 번만, 제목이 빈 링크(이미지 링크 등)는 다른 링크의 제목으로 대체."""
    p = _ListParser(base_url)
    p.feed(html)
    out: dict[str, dict] = {}
    for it in p.items:
        title = " ".join(it["title"].split())
        cur = out.setdefault(it["url"], {"url": it["url"], "title": "", "published_on": None})
        cur["title"] = cur["title"] or title
        cur["published_on"] = cur["published_on"] or it["published_on"]
    return [m for m in out.values() if m["title"]]


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "header", "footer"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "header", "footer") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(" ".join(data.split()))


def memo_text(html: str) -> str:
    p = _TextParser()
    p.feed(html)
    return "\n".join(p.parts)


def body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_memo_text(url: str, cfg: dict | None = None, fetch: Fetch = default_fetch) -> str | None:
    """요약할 때만 쓰는 일회용 본문(저장하지 않음). 확인 단계를 통과하지 못하면 None."""
    cfg = cfg or load_config()
    perm = check_permission(cfg, fetch)
    if not perm.allowed or not perm.robots.can_fetch(cfg.get("user_agent", "baseline"), url):
        return None
    return memo_text(fetch(url, cfg.get("user_agent", "baseline")))


def collect(cfg: dict | None = None, fetch: Fetch = default_fetch, sleep: Callable[[float], None] = time.sleep,
            path: Path | None = None) -> dict:
    """새 메모만 받아 memos에 멱등 저장. 반환: {status, reason, new, seen}."""
    cfg = cfg or load_config()
    store.init_db(path)
    perm = check_permission(cfg, fetch)
    if not perm.allowed:
        store.log_run(JOB, "manual_required", 0, perm.reason, path=path)
        return {"status": "manual_required", "reason": perm.reason, "new": 0, "seen": 0}
    ua = cfg.get("user_agent", "baseline")
    try:
        items = parse_memo_list(fetch(cfg["list_url"], ua), cfg["list_url"])
    except Exception as e:
        store.log_run(JOB, "fail", 0, f"목록 받기 실패: {type(e).__name__}", path=path)
        return {"status": "fail", "reason": str(e), "new": 0, "seen": 0}
    existing = store.get_memos(path)
    known = set(existing["url"]) if not existing.empty else set()
    new = 0
    for it in items:
        if it["url"] in known:
            continue  # 주 1회 — 이미 아는 메모는 다시 받지 않는다
        h = None
        if perm.robots.can_fetch(ua, it["url"]):
            sleep(float(cfg.get("request_interval_seconds", 3)))
            try:
                h = body_hash(memo_text(fetch(it["url"], ua)))  # 본문은 해시만 남기고 버린다
            except Exception:
                h = None
        new += store.upsert_memo({**it, "body_hash": h, "source": "auto"}, path=path)
    store.log_run(JOB, "ok", new, f"목록 {len(items)}건 중 새 메모 {new}건", path=path)
    return {"status": "ok", "reason": "", "new": new, "seen": len(items)}


def add_manual(title: str, url: str, published_on: str | None, summary: str | None = None,
               path: Path | None = None) -> bool:
    store.init_db(path)
    if published_on:
        date.fromisoformat(published_on)  # 형식 확인
    is_new = store.upsert_memo({"url": url, "title": title, "published_on": published_on, "source": "manual"}, path=path)
    if summary:
        store.set_memo_summary(url, summary, "manual", path=path)
    return is_new


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="하워드 막스 메모 수집(주 1회) / 수동 입력")
    p.add_argument("--manual", action="store_true")
    p.add_argument("--title")
    p.add_argument("--url")
    p.add_argument("--date", help="게시일 YYYY-MM-DD(모르면 생략)")
    p.add_argument("--summary", help="내가 쓴 한두 줄 요약(원문 인용 금지)")
    args = p.parse_args(argv)
    if args.manual:
        if not (args.title and args.url):
            p.error("--manual에는 --title, --url 필요")
        print("새 메모 기록" if add_manual(args.title, args.url, args.date, args.summary) else "이미 있는 메모(갱신)")
        return 0
    r = collect()
    print(f"[marks_memos] {r['status']}: {r['reason'] or ''} 새 메모 {r['new']}건")
    return 0 if r["status"] in ("ok", "manual_required") else 1


if __name__ == "__main__":
    sys.exit(main())
