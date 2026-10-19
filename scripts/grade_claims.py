"""
claims 테이블에 기록된 전문가 발언/소문을 사후 채점하는 CLI.

이 스크립트는 방향을 예측하지 않는다 — 이미 지난 발언이 목표가·기간 기준으로
맞았는지 사후에 채점할 뿐이다(CLAUDE.md 원칙 5). target_price·horizon_days가
없는 claim은 애초에 방향/기간을 명시하지 않은 발언이라 채점 대상이 아니다.

hit 판정 로직 (방향은 said_on 시점 price_then 대비 target_price 부호로만 판단한다):
    target_price > price_then (상승 방향) → price_after >= target_price 면 hit
    target_price < price_then (하락 방향) → price_after <= target_price 면 hit
    target_price == price_then (방향 불명) → hit=0으로 두되 사유를 별도 표시한다.
        설계 판단: 목표가가 발언 시점 가격과 같다는 건 애초에 방향을 제시하지
        않은 발언이라는 뜻이다. hit=1로 관대하게 주면 "아무 말이나 해도 맞힌 것"이
        되어 채점 자체가 무의미해지므로, 애매한 경우는 miss(0) 쪽으로 둔다.

만기 판정: said_on + horizon_days일(달력일)이 기준일(오늘 또는 --as-of)보다
과거이거나 같아야 채점 대상이다 — 기간이 아직 안 지난 claim을 미리 채점하지 않는다.

시세가 아직 DB에 없는(수집이 안 된) 날짜는 조용히 건너뛴다 — 잘못된 값으로
채점하는 것보다 건너뛰는 게 안전하다는 게 이 스크립트의 핵심 판단이다.

사용 예:
    python scripts/grade_claims.py --dry-run       # 미리보기, DB 안 건드림
    python scripts/grade_claims.py                 # 실제 채점
    python scripts/grade_claims.py --as-of 2026-08-01
    python scripts/grade_claims.py --force          # 이미 채점된 것도 다시 채점
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from db import store  # noqa: E402


def _price_after(ticker: str, on_or_after: date) -> tuple[str, float] | None:
    """
    on_or_after 날짜, 또는 그 이후 가장 가까운 거래일의 종가.
    해당 종목 시세가 DB에 아예 없거나, on_or_after 이후 시세가 아직 수집 안
    됐으면(=수집 파이프라인이 아직 이 날짜까지 못 갔으면) None.
    """
    s = store.get_prices(ticker)
    if s.empty:
        return None
    s = s.sort_index()
    on_or_after_prices = s[s.index >= pd.Timestamp(on_or_after)]
    if on_or_after_prices.empty:
        return None
    d = on_or_after_prices.index[0]
    return d.date().isoformat(), float(on_or_after_prices.iloc[0])


def _judge(price_then: float, target_price: float, price_after: float) -> tuple[int, str]:
    if target_price > price_then:
        return (1 if price_after >= target_price else 0), "상승 방향"
    if target_price < price_then:
        return (1 if price_after <= target_price else 0), "하락 방향"
    return 0, "방향 불명(target_price==price_then) — miss로 처리"


def _grade_candidates(candidates: pd.DataFrame, as_of: date) -> tuple[list[dict], Counter]:
    graded: list[dict] = []
    skipped: Counter = Counter()

    for row in candidates.itertuples():
        due_date = date.fromisoformat(row.said_on) + timedelta(days=int(row.horizon_days))
        if due_date > as_of:
            skipped["만기 미도래"] += 1
            continue
        if pd.isna(row.price_then):
            skipped["price_then 없음"] += 1
            continue

        result = _price_after(row.ticker, due_date)
        if result is None:
            skipped["시세 데이터 없음(수집 안 됨)"] += 1
            continue

        graded_on, price_after = result
        hit, reason = _judge(float(row.price_then), float(row.target_price), price_after)
        graded.append({
            "id": row.id, "who": row.who, "ticker": row.ticker,
            "due_date": due_date.isoformat(), "graded_on": graded_on,
            "price_then": float(row.price_then), "target_price": float(row.target_price),
            "price_after": price_after, "hit": hit, "reason": reason,
        })

    return graded, skipped


def main() -> None:
    p = argparse.ArgumentParser(description="claims 테이블 발언/소문 사후 채점")
    p.add_argument("--as-of", default=None, help="YYYY-MM-DD, 생략하면 오늘. 이 날짜 기준으로 만기 판정")
    p.add_argument("--force", action="store_true", help="이미 채점된 claim도 다시 채점")
    p.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 채점 결과 미리보기만 출력")
    args = p.parse_args()

    store.init_db()
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    candidates = store.get_claims_for_grading(include_graded=args.force)
    if candidates.empty:
        print("채점 대상 claim이 없습니다 (target_price·horizon_days가 모두 있는 건이 없음).")
        return

    graded, skipped = _grade_candidates(candidates, as_of)

    skip_summary = ", ".join(f"{reason} {n}건" for reason, n in skipped.items()) or "없음"
    print(f"채점 후보 {len(candidates)}건 중 {len(graded)}건 채점 가능 (건너뜀: {skip_summary})")

    for g in graded:
        mark = "적중" if g["hit"] else "실패"
        print(f"  [{mark}] id={g['id']} {g['who']} · {g['ticker']} · "
              f"목표 {g['target_price']} vs 사후 {g['price_after']:.2f} ({g['reason']})")

    if not graded:
        return

    if args.dry_run:
        print("\n--dry-run: DB에 기록하지 않았습니다.")
        return

    for g in graded:
        store.grade_claim(g["id"], g["graded_on"], g["price_after"], g["hit"])

    hits = sum(g["hit"] for g in graded)
    rate = hits / len(graded) * 100
    print(f"\n저장됨: {len(graded)}건 채점 완료, 적중 {hits}건 ({rate:.1f}%)")
    if len(graded) < store.MIN_SAMPLES_CLAIMS:
        print(f"⚠ 표본 {len(graded)}건은 통계적으로 유의미하지 않을 수 있습니다 "
              f"(권장 {store.MIN_SAMPLES_CLAIMS}건 이상).")


if __name__ == "__main__":
    main()
