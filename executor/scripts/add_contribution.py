"""
적립 기록(수동) — 매달 넣은 돈과 어느 계좌에 넣었는지, 적립 계좌 평가금액을 기록한다.

**executor는 돈을 옮기지 않는다.** 이 스크립트는 사람이 직접 옮긴 사실을 executor.db에 적기만 한다
(append-only). 자동매매 계좌에 넣은 돈은 계좌 평가금액을 늘릴 뿐 운용 한도를 늘리지 않는다 — 한도는
승격으로만 바뀐다(EXECUTOR.md).

사용:
    python -m executor.scripts.add_contribution contribution --date 2026-10-25 --amount 1000000 --to savings
    python -m executor.scripts.add_contribution valuation --as-of 2026-10-31 --value 3120000
    python -m executor.scripts.add_contribution list
"""

from __future__ import annotations

import argparse
import sys

from executor import store


def main(argv: list[str] | None = None, db_path=None) -> int:
    p = argparse.ArgumentParser(description="적립 계좌·자동매매 계좌 납입 기록(수동, 돈을 옮기지 않음)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("contribution", help="납입 기록")
    c.add_argument("--date", required=True)
    c.add_argument("--amount", type=float, required=True, help="원")
    c.add_argument("--to", required=True, choices=["savings", "trading"], help="savings=적립 계좌, trading=자동매매 계좌")
    c.add_argument("--note")
    v = sub.add_parser("valuation", help="적립 계좌 평가금액 기록")
    v.add_argument("--as-of", required=True)
    v.add_argument("--value", type=float, required=True)
    v.add_argument("--note")
    sub.add_parser("list", help="기록 보기")
    args = p.parse_args(argv)

    store.init_db(db_path)
    if args.cmd == "contribution":
        store.add_contribution(args.date, args.amount, args.to, args.note, path=db_path)
        print(f"기록: {args.date} {args.amount:,.0f}원 → {'적립 계좌' if args.to == 'savings' else '자동매매 계좌'}")
    elif args.cmd == "valuation":
        store.add_savings_valuation(args.as_of, args.value, args.note, path=db_path)
        print(f"기록: 적립 계좌 평가금액 {args.as_of} {args.value:,.0f}원")
    else:
        df = store.contributions(path=db_path)
        if df.empty:
            print("납입 기록 없음")
        else:
            for _, r in df.iterrows():
                print(f"{r['date']} {r['amount_krw']:>12,.0f}원 → {r['destination']}  {r['note'] or ''}")
        val = store.latest_savings_valuation(path=db_path)
        shown = f"{val['value_krw']:,.0f}원 ({val['as_of']})" if val else "데이터 없음"
        print(f"적립 계좌 평가금액: {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
