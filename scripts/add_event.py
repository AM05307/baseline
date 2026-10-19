"""
수급 이벤트 수동 입력 — 자사주 매입 기간·실적발표일·콘퍼런스콜 일정.

이벤트는 신호가 아니라 **기록**이다. 입력한 이벤트는 대시보드 "주시할 이벤트" 카드에 앞으로의 일정으로
나오고, 이미 지난 자사주 매입은 이벤트 스터디(analysis/event_study.py)에 쓰인다.

멱등이다(원칙 6): (종목, 종류, 시작일, 종료일)이 같으면 같은 이벤트라 다시 넣어도 행이 늘지 않고,
제목·메모는 **새로 준 값이 있을 때만** 갱신한다. 삭제 기능은 일부러 없다(잘못 넣었으면 SQL로 직접).

사용:
    # 자사주 매입 기간 (종료일 필수 — 기간 이벤트)
    python scripts/add_event.py --type buyback --start 2026-03-04 --end 2026-06-03 --note "취득 예정 N주"
    # 실적발표일 / 콘퍼런스콜 (단일 날짜)
    python scripts/add_event.py --type earnings  --start 2026-10-23 --title "3분기 실적"
    python scripts/add_event.py --type conf_call --start 2026-10-23 --note "한국시간 오후 4시(예시)"

    python scripts/add_event.py --list                 # 등록된 이벤트 보기
    python scripts/add_event.py --type buyback --start ... --end ... --dry-run   # 검증만, 저장 안 함
    python scripts/add_event.py --from-dart            # 이미 수집된 공시에서 추출(네트워크 호출 없음)

--from-dart는 DB에 있는 disclosures만 본다. 공시검색 목록에는 취득 예정 기간이 없어서, 기간을
아는 상세 공시를 별도로 받아오지 않은 한 이벤트로 만들지 않고 "기간 미확인 N건"으로만 알려준다
(기간을 지어내지 않음). 상세 조회는 DART 키가 필요하고 이 스크립트는 호출하지 않는다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import events as ev  # noqa: E402
from db import store  # noqa: E402

DEFAULT_TICKER = "000660"


def _print_events(df) -> None:
    if df.empty:
        print("등록된 이벤트가 없습니다.")
        return
    for _, r in df.iterrows():
        period = r["start_date"] if r["start_date"] == r["end_date"] else f"{r['start_date']} ~ {r['end_date']}"
        label = ev.EVENT_TYPES.get(r["event_type"], r["event_type"])
        extra = " · ".join(x for x in (r["title"], r["note"]) if isinstance(x, str) and x)
        print(f"  [{r['ticker']}] {label:<8s} {period:<23s} ({r['source']}){(' — ' + extra) if extra else ''}")


def run_from_dart(ticker: str, dry_run: bool) -> int:
    """이미 DB에 있는 공시에서 추출. 기간을 아는 상세가 없으므로(details=None) 현재는 전부 미확인으로 나온다."""
    disclosures = store.get_disclosures(stock_code=ticker)
    result = ev.extract_buyback_events(disclosures, details=None)
    print(f"[{ticker}] 자기주식 취득 결정 계열 공시 {len(result.events) + len(result.unresolved)}건 발견")
    for u in result.unresolved:
        print(f"  기간 미확인({u['reason']}): {u['rcept_dt']} {u['report_nm']} [{u['rcept_no']}]")
    saved = 0
    for e in result.events:
        if dry_run:
            print(f"  (dry-run) {e['start_date']} ~ {e['end_date']} {e['title']}")
        else:
            store.upsert_event(e)
            saved += 1
    return saved


def main() -> None:
    p = argparse.ArgumentParser(description="수급 이벤트 입력 (자사주 매입·실적발표·콘퍼런스콜)")
    p.add_argument("--type", dest="event_type", choices=sorted(ev.EVENT_TYPES), help="이벤트 종류")
    p.add_argument("--start", help="시작일 YYYY-MM-DD")
    p.add_argument("--end", help="종료일 YYYY-MM-DD (buyback은 필수, 그 외 생략하면 시작일과 같음)")
    p.add_argument("--ticker", default=DEFAULT_TICKER)
    p.add_argument("--title")
    p.add_argument("--note")
    p.add_argument("--dry-run", action="store_true", help="검증만 하고 저장하지 않음")
    p.add_argument("--list", action="store_true", help="등록된 이벤트 목록")
    p.add_argument("--from-dart", action="store_true", help="수집된 공시에서 자사주 취득 이벤트 추출(네트워크 없음)")
    args = p.parse_args()

    store.init_db()

    if args.list:
        _print_events(store.get_events(args.ticker))
        return
    if args.from_dart:
        saved = run_from_dart(args.ticker, args.dry_run)
        print(f"{saved}건 저장됨." if not args.dry_run else "--dry-run: 저장하지 않았습니다.")
        return
    if not args.event_type or not args.start:
        p.error("--type 과 --start 가 필요합니다 (또는 --list / --from-dart)")

    try:
        row = ev.validate_event({
            "ticker": args.ticker, "event_type": args.event_type, "start_date": args.start,
            "end_date": args.end, "title": args.title, "note": args.note, "source": "manual",
        })
    except ValueError as e:
        print(f"입력 오류: {e}")
        sys.exit(2)

    if args.dry_run:
        print(f"--dry-run: 검증 통과, 저장하지 않았습니다 → {row}")
        return
    outcome = store.upsert_event(row)
    print(f"{'새로 저장' if outcome == 'inserted' else '이미 있는 이벤트 — 갱신'}: "
          f"{ev.EVENT_TYPES[row['event_type']]} {row['start_date']} ~ {row['end_date']}")


if __name__ == "__main__":
    main()
