"""
db.disclosures(collectors/dart_client.py가 채움)의 공시를 claims 테이블에
기록하는 CLI — scripts/add_claim.py와 같은 경로(claims 테이블)로 들어가되,
사람이 손으로 입력하는 게 아니라 이미 수집된 공시 목록에서 기계적으로
옮긴다(4단계: "공시 → 뉴스/소문 카드").

**confidence="official"** — claims.confidence는 지금까지 'news'(출처
확인)·'rumor'(미확인) 두 값만 썼는데, 공시는 둘 다 아니다(DART 공식
전자공시 자체가 출처이자 원문이다, 사람의 발언/소문이 아님). 그래서 세
번째 값으로 'official'을 쓴다 — DB에 CHECK 제약은 없어서(claims 스키마
주석 참고) 코드 추가 없이 그대로 쓸 수 있다. 대시보드(web/main.py)의
"미확인" 라벨 로직은 confidence=='rumor'일 때만 발동하므로 'official'은
그 경고가 안 붙는다(의도한 동작 — 공시는 미확인 소문이 아니다).

summary는 공시 "보고서명"(report_nm, 예: "분기보고서 (2026.03)")을 그대로
적는다 — 이건 전문가의 발언 원문이 아니라 DART가 공식적으로 붙인 문서
제목(메타데이터)이라 원칙 5("원문 인용 금지")의 대상이 아니다(뉴스
헤드라인을 그대로 저장하는 collectors/news.py와 같은 취급).

**원문 링크**: DART 전자공시 뷰어 URL(https://dart.fss.or.kr/dsaf001/
main.do?rcpNo=...)을 source_url에 넣는다 — 이 URL 패턴은 널리 알려진
DART 공개 뷰어 링크 형식이다(API 엔드포인트가 아니라 사람이 보는 웹
페이지라 TODO 표시 없음, 비교적 확실).

**멱등성**: claims는 append-only라 DB가 중복을 막아주지 않는다. 그래서 이
스크립트가 쓰기 전에 db.store.claim_source_urls()로 이미 기록된 source_url
집합을 확인하고, 같은 공시(같은 rcept_no -> 같은 URL)를 다시 기록하지
않는다 — 몇 번을 다시 돌려도 안전하다(원칙 6, 스크립트 레이어에서 지킴).

price_then은 공시 접수일(rcept_dt) 기준 그 날 또는 그 이전 가장 최근 종가를
쓴다("오늘 기록한다"는 add_claim.py의 기본 동작과 달리, 이 스크립트는
**과거 공시를 지금 돌려서 기록**하므로 당시 가격을 써야 미래참조가 안 됨).
해당 날짜 전 시세가 DB에 없으면 비워둔다(지어내지 않음).

사용:
    python scripts/seed_disclosure_claims.py --stock-code 000660
    python scripts/seed_disclosure_claims.py --stock-code 000660 --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from db import store  # noqa: E402

DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def _price_as_of(ticker: str, date: str) -> float | None:
    """ticker의 date(또는 그 이전 가장 최근) 종가. 시세 자체가 없으면 None(지어내지 않음)."""
    s = store.get_prices(ticker)
    if s.empty:
        return None
    ts = pd.Timestamp(date)
    prior = s.index[s.index <= ts]
    if len(prior) == 0:
        return None
    return float(s.loc[prior.max()])


def build_claim_row(disclosure: dict) -> dict:
    """disclosures 한 행(dict) -> store.insert_claim()이 받는 claim 행."""
    rcept_no = disclosure["rcept_no"]
    stock_code = disclosure.get("stock_code")
    rcept_dt = disclosure["rcept_dt"]
    rcept_dt_str = rcept_dt.date().isoformat() if hasattr(rcept_dt, "date") else str(rcept_dt)
    return {
        "said_on": rcept_dt_str,
        "who": disclosure.get("flr_nm") or disclosure.get("corp_name") or "DART 전자공시",
        "affiliation": disclosure.get("corp_name"),
        "ticker": stock_code or disclosure.get("corp_code") or "UNKNOWN",
        "summary": f"공시: {disclosure.get('report_nm') or '(제목 없음)'}",
        "stance": None,
        "source_url": DART_VIEWER_URL.format(rcept_no=rcept_no),
        "price_then": _price_as_of(stock_code, rcept_dt_str) if stock_code else None,
        "target_price": None,
        "horizon_days": None,
        "confidence": "official",
    }


def seed_from_disclosures(stock_code: str | None = None, dry_run: bool = False) -> dict[str, int]:
    """
    disclosures 테이블(이미 수집된 것만, 네트워크 호출 없음)을 훑어서
    아직 claims에 없는 공시만 기록한다. 반환: {"seeded": n, "skipped_existing": n}.
    """
    store.init_db()
    disclosures_df = store.get_disclosures(stock_code=stock_code)
    if disclosures_df.empty:
        return {"seeded": 0, "skipped_existing": 0}

    existing_urls = store.claim_source_urls()
    seeded, skipped = 0, 0
    for _, row in disclosures_df.iterrows():
        claim_row = build_claim_row(row.to_dict())
        if claim_row["source_url"] in existing_urls:
            skipped += 1
            continue
        if not dry_run:
            store.insert_claim(claim_row)
            existing_urls.add(claim_row["source_url"])  # 같은 실행 안에서 중복 방지
        seeded += 1
    return {"seeded": seeded, "skipped_existing": skipped}


def main() -> None:
    p = argparse.ArgumentParser(description="disclosures 테이블의 공시를 claims에 시딩(confidence='official')")
    p.add_argument("--stock-code", default=None, help="이 종목코드의 공시만(생략하면 전체 disclosures)")
    p.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 몇 건이 새로 기록될지만 보여준다")
    args = p.parse_args()

    result = seed_from_disclosures(stock_code=args.stock_code, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}새로 기록: {result['seeded']}건, 이미 있어서 건너뜀: {result['skipped_existing']}건")
    if result["seeded"] == 0 and result["skipped_existing"] == 0:
        print("disclosures 테이블이 비어 있습니다 — 먼저 collectors/dart_client.py로 공시를 수집하세요(DART_API_KEY 필요).")


if __name__ == "__main__":
    main()
