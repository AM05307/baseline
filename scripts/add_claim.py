"""
전문가 발언·소문을 claims 테이블에 기록하는 CLI.

이 스크립트는 "신호"를 만들지 않는다 — 누가 뭐라고 했는지, 그때 주가가
얼마였는지를 기록해둘 뿐이다. 나중에 맞았는지 틀렸는지 채점하는 게 목적이다.
(CLAUDE.md 원칙 5)

confidence를 반드시 명시한다:
    news  — 출처(기사)가 있는 발언
    rumor — 출처가 불확실한 소문. 화면에 항상 "미확인" 라벨이 붙는다.

사용 예:
    python scripts/add_claim.py \\
        --who "어느 증권사 리포트" --affiliation "어느증권" --ticker 000660 \\
        --summary "메모리 업사이클 진입 초입이라는 취지" --stance 매수 \\
        --confidence news --source-url https://example.com/article \\
        --target-price 400000 --horizon-days 90

    python scripts/add_claim.py \\
        --who "어느 유튜브 채널" --affiliation "어느유튜브" --ticker 000660 \\
        --summary "단기 변동성 확대 경계 취지" --stance 중립 \\
        --confidence news --source-url https://youtube.com/watch?v=...

    python scripts/add_claim.py \\
        --who "온라인 커뮤니티 루머" --ticker 000660 \\
        --summary "3분기 실적 서프라이즈설" \\
        --confidence rumor

price_then은 --price-then으로 직접 주지 않으면, ticker에 해당하는 최신 종가를
DB에서 찾아 자동으로 채운다 (없으면 비워둠).

affiliation(소속)·stance(매수/중립/매도)는 분류 라벨일 뿐이다 — summary는
여전히 본인이 요약한 한 줄이어야 한다. 원문을 그대로 베끼거나 본인이 하지
않은 말을 지어내 넣지 말 것(원칙 5).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402


def _latest_close(ticker: str) -> float | None:
    s = store.get_prices(ticker)
    if s.empty:
        return None
    return float(s.iloc[-1])


def main() -> None:
    p = argparse.ArgumentParser(description="claims 테이블에 발언/소문 기록")
    p.add_argument("--who", required=True, help="발언자/출처 (예: '어느 리포트', '온라인 루머')")
    p.add_argument("--affiliation", default=None, help="소속 (예: '어느증권', '어느유튜브')")
    p.add_argument("--ticker", required=True, help="종목 코드 (예: 000660, SKHY, NVDA)")
    p.add_argument("--summary", required=True, help="본인이 요약한 한 줄. 원문 인용 금지.")
    p.add_argument("--stance", choices=["매수", "중립", "매도"], default=None,
                    help="의견 분류 라벨 (매수/중립/매도). 생략 가능.")
    p.add_argument("--confidence", choices=["news", "rumor"], required=True,
                    help="news=출처 있는 뉴스, rumor=미확인 소문")
    p.add_argument("--source-url", default=None, help="리포트/유튜브 등 출처 URL")
    p.add_argument("--target-price", type=float, default=None)
    p.add_argument("--horizon-days", type=int, default=None)
    p.add_argument("--price-then", type=float, default=None,
                    help="생략하면 DB에 있는 해당 종목 최신 종가를 자동으로 넣는다")
    p.add_argument("--said-on", default=None, help="YYYY-MM-DD, 생략하면 오늘")
    args = p.parse_args()

    store.init_db()
    price_then = args.price_then
    if price_then is None:
        price_then = _latest_close(args.ticker)
        if price_then is None:
            print(f"  참고: '{args.ticker}' 시세가 DB에 없어서 price_then을 못 채웠습니다.")

    store.insert_claim({
        "said_on": args.said_on or date.today().isoformat(),
        "who": args.who,
        "affiliation": args.affiliation,
        "ticker": args.ticker,
        "summary": args.summary,
        "stance": args.stance,
        "source_url": args.source_url,
        "price_then": price_then,
        "target_price": args.target_price,
        "horizon_days": args.horizon_days,
        "confidence": args.confidence,
    })

    tag = "⚠ 미확인 소문" if args.confidence == "rumor" else "출처 있는 뉴스"
    who_label = f"{args.who} ({args.affiliation})" if args.affiliation else args.who
    stance_label = f" · {args.stance}" if args.stance else ""
    print(f"기록됨 [{tag}] {who_label} · {args.ticker}{stance_label} · price_then={price_then}")


if __name__ == "__main__":
    main()
