"""
뉴스 헤드라인 수집기.

yfinance의 Ticker.news를 쓴다 — 별도 API 키 없이 Yahoo Finance가 모아놓은
뉴스 메타데이터(제목·출처·링크·게재일)를 가져올 수 있다. 본문은 저장하지 않는다
(저작권 문제 — 원문 인용 금지 원칙은 claims 테이블과 동일하게 적용).

주의: yfinance의 news 응답 스키마가 버전에 따라 두 가지로 나뉜다.
  - 최근 버전(≈0.2.38+): {"content": {"title", "summary", "pubDate",
    "provider": {"displayName"}, "canonicalUrl": {"url"}}}
  - 이전 버전: {"title", "publisher", "link", "providerPublishTime"(unix)}
_normalize()가 둘 다 처리한다. 이 세션에서는 실제 네트워크 호출을 못 해봐서
(pypi/야후 접근이 막힌 샌드박스) 두 스키마 다 문서 기준으로 방어적으로 짰다 —
로컬에서 실행해보고 실제 응답 구조가 다르면 이 함수만 고치면 된다.

모든 항목은 confidence='news'로 들어간다 (출처가 있는 뉴스). '소문'을 넣고
싶으면 scripts/add_claim.py로 claims 테이블에 confidence='rumor'로 직접
기록할 것 — 이 수집기가 자동으로 미확인 소문을 만들어내지는 않는다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf  # noqa: E402

from db import store  # noqa: E402

WATCH_TICKERS = {
    "000660.KS": "000660",
    "SKHY": "SKHY",
    "^SOX": "SOX",
    "NVDA": "NVDA",
}


def _normalize(item: dict, ticker: str) -> dict | None:
    content = item.get("content")
    if isinstance(content, dict):
        title = content.get("title")
        provider = content.get("provider") or {}
        source = provider.get("displayName") if isinstance(provider, dict) else None
        canonical = content.get("canonicalUrl") or {}
        url = canonical.get("url") if isinstance(canonical, dict) else content.get("clickThroughUrl")
        pub = content.get("pubDate")
        published_at = pub if pub else datetime.now(timezone.utc).isoformat()
    else:
        title = item.get("title")
        source = item.get("publisher")
        url = item.get("link")
        ts = item.get("providerPublishTime")
        published_at = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if isinstance(ts, (int, float))
            else datetime.now(timezone.utc).isoformat()
        )

    if not title:
        return None
    return {
        "ticker": ticker,
        "headline": title,
        "source": source,
        "url": url,
        "published_at": published_at,
        "confidence": "news",
    }


def fetch(symbol: str, ticker_label: str, limit: int = 10) -> list[dict]:
    raw = yf.Ticker(symbol).news or []
    out = []
    for item in raw[:limit]:
        norm = _normalize(item, ticker_label)
        if norm:
            out.append(norm)
    return out


def collect_all(limit_per_ticker: int = 10) -> int:
    store.init_db()
    total = 0
    for symbol, label in WATCH_TICKERS.items():
        try:
            rows = fetch(symbol, label, limit_per_ticker)
            n = store.upsert_news(rows)
            total += n
            print(f"  {symbol:10s} {n:3d} headlines")
        except Exception as e:
            print(f"  {symbol:10s} FAILED: {e}")
            store.log_run("news", "fail", 0, f"{symbol}: {e}")
    store.log_run("news", "ok", total)
    return total


if __name__ == "__main__":
    print("[news] 수집 시작")
    print(f"완료: {collect_all()} headlines")
