"""
승격 리포트 — 현재 단계의 성과를 config/promotion_criteria.yaml과 비교해서 항목별 PASS/FAIL을 보여준다.

**승격을 반영하지 않는다.** "승격 가능"이라고 표시만 하고, 실제 승격은 사람이
`python -m executor.scripts.promote_tier`로 확인 문구를 입력해야 반영된다. 결과는 executor.db
promotion_results에 append-only로 남는다. 주문 없음, 네트워크 없음.

사용:
    python -m executor.scripts.promotion_report
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from executor import config_files, ladder_ops, metrics, store
from executor.criteria import promotion_status


def _fmt(v) -> str:
    if v is None:
        return "데이터 없음"
    return f"{v:,.2f}" if isinstance(v, float) and not float(v).is_integer() else f"{v:,.0f}"


def _p(v) -> str:
    return "데이터 없음" if v is None else f"{v:+.2f}%"


def run(db_path=None) -> int:
    store.init_db(db_path)
    ladder = config_files.load_ladder(db_path=db_path)
    criteria = config_files.load_criteria(db_path=db_path)
    ladder_ops.ensure_initialized(ladder, date.today().isoformat(), db_path)
    bench = metrics.baseline_benchmarks(config_files.load_executor_config(db_path=db_path)["benchmarks"])
    cur, m = ladder_ops.current_metrics(ladder, bench, db_path)
    st = promotion_status(ladder, criteria, cur["to_tier"], m)
    store.append_promotion_result(st.tier, st.next_tier, st.eligible, {**st.to_dict(), "via": "promotion_report"},
                                  path=db_path)

    print(f"현재 단계: {st.tier} (진입 {cur['effective_on']}) → 다음 단계: {st.next_tier or '없음'}")
    print(f"표본: 운용 {m.days}일 · 거래 {m.trades}건 · 주문 오류 {m.order_errors}건 · 잔고 불일치 {m.reconciliation_mismatches}건")
    print(f"단계 내 누적수익률 {_p(m.cum_return_pct)} · MDD {_p(m.mdd_pct)}")
    for name, b in m.benchmarks.items():
        print(f"  같은 기간 단순 보유 {name}: {_p(b['return_pct'])} (MDD {_p(b['mdd_pct'])})")
    for n in m.notes:
        print(f"  참고: {n}")
    print(f"\n승격 기준 '{st.stage}' ({'확정' if st.criteria_confirmed else '미확정'})")
    for i in st.items:
        print(f"  [{'PASS' if i.passed else 'FAIL'}] {i.label}: 실제 {_fmt(i.actual)} / 기준 {_fmt(i.threshold)}"
              f"{' — ' + i.note if i.note else ''}")
    if st.eligible:
        print(f"\n→ 승격 가능. 반영하려면 직접 실행: python -m executor.scripts.promote_tier")
    else:
        print("\n→ 승격 불가:")
        for b in st.blockers:
            print(f"  - {b}")
    print("\n과거 운용 기록에 대한 측정이며 향후 수익을 보장하지 않습니다.")
    return 0


def main() -> None:
    argparse.ArgumentParser(description="자본 사다리 승격 리포트(반영 안 함)").parse_args()
    sys.exit(run())


if __name__ == "__main__":
    main()
