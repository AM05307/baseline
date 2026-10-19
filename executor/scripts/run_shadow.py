"""
섀도 실행 — 오늘(KST) 장 마감 후 한 번. 실제 주문·브로커 호출 없음(섀도 원장 안에서만 체결 처리).

먼저 baseline 데이터를 최신으로:
    python collectors/collect_daily.py
    python scripts/compute_signals.py
그다음:
    python -m executor.scripts.run_shadow
    python -m executor.scripts.run_shadow --status     # 최근 실행·섀도 주문 보기

휴장일·캘린더 범위 밖·장 마감 전·데이터 오래됨·킬 스위치·정지 상태면 실행하지 않고 사유만 기록한다.
같은 날 여러 번 돌려도 섀도 주문은 한 번만 기록된다.
"""

from __future__ import annotations

import argparse
import sys

from executor import shadow, store


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="섀도 실행(오늘 장 마감 후, 주문 없음)")
    p.add_argument("--status", action="store_true", help="실행하지 않고 최근 기록만 보기")
    args = p.parse_args(argv)
    store.init_db()
    if not args.status:
        out = shadow.run_shadow()
        print(f"섀도 실행: {out.status}" + (f" — {out.reason}" if out.reason else ""))
        for w in out.warnings:
            print(f"  ⚠ 경고: {w}")
        if out.status == "ok":
            print(f"  위험 점수 {out.risk_score:.1f} · 단계 {out.stage} · 목표 비중 {out.target_weight:.0%}")
            for o in out.orders:
                print(f"  보냈을 주문: {o.side} {o.ticker} {o.qty}주 @ {o.ref_price:,.0f}")
            if not out.orders:
                print("  보냈을 주문 없음(목표 비중과 같음)")
    runs = store.shadow_runs().tail(10)
    print("\n최근 섀도 실행:")
    for _, r in runs.iterrows():
        print(f"  {r['decision_date']} {r['status']:<20} {r['reason'] or ''}")
    return 0 if args.status or out.status in ("ok",) or out.status.startswith("skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
