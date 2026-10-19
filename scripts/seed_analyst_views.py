"""
2026-07~08 발언 기록을 claims 테이블에 등록하는 1회성 시딩 스크립트.

전부 사용자가 영상을 보고 직접 전달한 요지를 그대로 옮긴 것이다 — 원문을
인용하지 않고(원칙 5), 이 스크립트가 문장을 지어내지도 않는다. 각 항목에
출처 영상·회차(source_ref)를 반드시 남기고, 촬영일이 추정인 항목은
date_confidence='estimated'로 표시한다.

horizon_days: 발언에서 기간을 명시하지 않은 경우 63거래일(사용자 지정 기본
채점 기준)을 달력일로 환산해 90일을 쓴다(63거래일 ≈ 63*7/5 ≈ 88.2일,
90일로 올림 — analysis/signal_backtest.py의 DEFAULT_HORIZONS=63과 같은
취지). target_price가 없는 항목(대부분의 정성적 견해)은 현재 채점 로직
(scripts/grade_claims.py, target_price 필요)으로는 채점되지 않는다 —
숫자를 지어내서 억지로 채점 대상으로 만들지 않았다(확인 필요로 남김).

멱등이 아니다 — claims.insert_claim은 append-only라 이 스크립트를 두 번
돌리면 같은 발언이 두 번 저장된다. 그래서 1회만 실행할 것(또는 실행 전
해당 who의 기존 행을 지우고 다시 실행).

사용:
    python scripts/seed_analyst_views.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402

HYNIX = "000660"
HORIZON_63_TRADING_DAYS_AS_CALENDAR_DAYS = 90

VIEWS = [
    {
        "said_on": "2026-07-13", "who": "전문가 A", "affiliation": "증권사(비공개)",
        "summary": "아직 고점은 아님, 산업은 커지나 주가엔 파도",
        "source_ref": "방송 A-1", "date_confidence": "confirmed",
    },
    {
        "said_on": "2026-07-13", "who": "KB증권", "affiliation": "KB증권",
        "summary": "SK하이닉스 목표가 420만원 (전문가 A가 방송 A-1에서 언급 — "
                   "원 리포트 날짜는 확인 안 됨, said_on은 언급 시점)",
        "target_price": 4_200_000.0,
        "source_ref": "방송 A-1 (전문가 A 언급)", "date_confidence": "estimated",
    },
    {
        "said_on": "2026-07-13", "who": "BNK투자증권", "affiliation": "BNK투자증권",
        "summary": "SK하이닉스 목표가 185만원 (전문가 A가 방송 A-1에서 언급 — "
                   "원 리포트 날짜는 확인 안 됨, said_on은 언급 시점)",
        "target_price": 1_850_000.0,
        "source_ref": "방송 A-1 (전문가 A 언급)", "date_confidence": "estimated",
    },
    {
        "said_on": "2026-07-13", "who": "모건스탠리", "affiliation": "모건스탠리",
        "summary": "메모리→하이퍼스케일러 비중 이동 보고서 (전문가 A가 방송 A-1에서 "
                   "언급 — 원 리포트 날짜는 확인 안 됨, said_on은 언급 시점)",
        "source_ref": "방송 A-1 (전문가 A 언급)", "date_confidence": "estimated",
    },
    {
        "said_on": "2026-06-30", "who": "전문가 B", "affiliation": "전 증권사 리서치",
        "summary": "변동성 지속, 현금 30~50% 버퍼",
        "source_ref": "방송 B-1", "date_confidence": "confirmed",
    },
    {
        "said_on": "2026-08-01", "who": "전문가 B", "affiliation": "전 증권사 리서치",
        "summary": "코스피 7,500~8,000 가능, 연내 전고점은 어려움, 반도체가 가격(P)→"
                   "물량(Q) 사이클로 전환할 확률 60~70%, 가을~내년 봄 박스권",
        "source_ref": "방송 B-2 (2026-08 촬영 추정, 정확한 날짜 미상)",
        "date_confidence": "estimated",
    },
    {
        "said_on": "2026-08-01", "who": "전문가 C", "affiliation": "투자자문사(비공개)",
        "summary": "반도체 실적 3~4년 호조는 확실, 하락은 비싸서가 아니라 국내 자금 "
                   "소진 때문, 2028년 실적 확신이 생기면 전고점 향하나 매물대로 시간 "
                   "걸림, 한국 기준금리 2~3회 추가 인상 예상",
        "source_ref": "방송 C-1 (2026-08 촬영 추정, 정확한 날짜 미상)",
        "date_confidence": "estimated",
    },
]


def main() -> None:
    store.init_db()
    for row in VIEWS:
        row.setdefault("ticker", HYNIX)
        row.setdefault("horizon_days", HORIZON_63_TRADING_DAYS_AS_CALENDAR_DAYS)
        row.setdefault("confidence", "news")
        store.insert_claim(row)
        print(f"기록됨: {row['who']} · {row['said_on']} · {row['summary'][:30]}...")
    print(f"\n총 {len(VIEWS)}건 기록. 다시 실행하면 중복 저장되니 1회만 돌릴 것.")


if __name__ == "__main__":
    main()
