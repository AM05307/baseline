"""
수동 승격 — 사람이 직접 실행하고 확인 문구를 정확히 입력해야 반영된다.

승격 시점에 기준을 다시 계산한다(과거 리포트의 PASS만 믿지 않음). 기준·다음 단계가 미확정이거나 FAIL 항목이
있거나 계좌가 정지(halt) 상태면 거부한다. 실제 자금 단계로 올릴 때는 자동매매 계좌 평가금액을 직접
입력해야 한다(--account-value) — 운용분 배정액 = min(계좌 평가금액, 다음 단계 한도).

executor는 돈을 옮기지 않는다. 배정은 executor 원장 안의 숫자일 뿐이고, 계좌에 돈을 넣는 건 사람이 한다.

사용:
    python -m executor.scripts.promote_tier
    python -m executor.scripts.promote_tier --account-value 3500000
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from executor import config_files, ladder_ops, metrics, store


def run(account_value: float | None, confirm_text: str | None = None, effective_on: str | None = None,
        db_path=None, input_fn=input) -> int:
    store.init_db(db_path)
    ladder = config_files.load_ladder(db_path=db_path)
    criteria = config_files.load_criteria(db_path=db_path)
    today = effective_on or date.today().isoformat()
    ladder_ops.ensure_initialized(ladder, today, db_path)
    cur = store.current_tier(db_path)["to_tier"]
    nxt = ladder.next(cur)
    if nxt is None:
        print("마지막 단계입니다.")
        return 1
    phrase = ladder_ops.confirm_phrase(cur, nxt.name)
    if confirm_text is None:
        print(f"{cur} → {nxt.name} 승격을 요청합니다. 계속하려면 아래 문구를 정확히 입력하세요:\n  {phrase}")
        confirm_text = input_fn("> ")
    held = store.internal_positions(nxt.account, db_path)
    prices = metrics.baseline_latest_prices(held)
    try:
        st = ladder_ops.promote(ladder, criteria, today, confirm_text, prices, account_value,
                                metrics.baseline_benchmarks(config_files.load_executor_config(db_path=db_path)["benchmarks"]),
                                db_path)
    except (ladder_ops.PromotionRefused, ValueError) as e:
        print(f"승격 거부: {e}")
        return 2
    print(f"승격 반영: {st.tier} → {st.next_tier} ({today})")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="자본 사다리 수동 승격(확인 문구 필요)")
    p.add_argument("--account-value", type=float, default=None, help="실제 자금 단계로 올릴 때 자동매매 계좌 평가금액(원)")
    args = p.parse_args()
    sys.exit(run(args.account_value))


if __name__ == "__main__":
    main()
