"""
수동 입력값(금리 단계·마이크론 매출총이익률·000660 선행 PBR·이벤트 날짜) CLI.

db.store.insert_manual_input()은 append-only다 — 같은 input_type을 다시
입력해도 덮어쓰지 않고 새 행이 쌓인다("입력 이력 남기기"). "지금 유효한 값"은
analysis/signals.py의 수동 입력 의존 신호가 db.store.get_manual_timeline()
결과를 날짜 인덱스에 forward-fill해서 판단한다 — effective_on이 가장 최근인
값이 그 날 이후로 계속 유효하다고 본다.

사용 예:
    # 금리 단계 전환 (RATE_STAGES: 인상 전/1회/2~3회/이전 고점 도달)
    python scripts/add_manual.py rate-stage --stage "2~3회" --effective-on 2026-09-01

    # 마이크론 분기 매출총이익률(%)
    python scripts/add_manual.py memory-margin --value 58.3 --effective-on 2026-09-25 \\
        --note "FY26 Q4 실적발표"

    # 000660 선행 PBR
    python scripts/add_manual.py pbr --value 1.05 --effective-on 2026-10-01

    # 이벤트 날짜(가중치 없이 캘린더에 표시만, section 3)
    python scripts/add_manual.py event --effective-on 2026-10-29 \\
        --note "미국 FOMC"
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.signals import RATE_STAGES  # noqa: E402
from db import store  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="수동 입력값 기록 (append-only, 입력 이력 보존)")
    sub = p.add_subparsers(dest="input_type", required=True)

    rate = sub.add_parser("rate-stage", help="미국 기준금리 단계")
    rate.add_argument("--stage", required=True, choices=RATE_STAGES)
    rate.add_argument("--effective-on", default=None, help="YYYY-MM-DD, 생략하면 오늘")
    rate.add_argument("--note", default=None)

    margin = sub.add_parser("memory-margin", help="마이크론 분기 매출총이익률(%%)")
    margin.add_argument("--value", type=float, required=True)
    margin.add_argument("--effective-on", default=None)
    margin.add_argument("--note", default=None)

    pbr = sub.add_parser("pbr", help="000660 선행 PBR")
    pbr.add_argument("--value", type=float, required=True)
    pbr.add_argument("--effective-on", default=None)
    pbr.add_argument("--note", default=None)

    event = sub.add_parser("event", help="이벤트 날짜 (가중치 없음, 캘린더 표시용)")
    event.add_argument("--effective-on", required=True)
    event.add_argument("--note", required=True, help="이벤트 설명")

    args = p.parse_args()
    store.init_db()
    effective_on = getattr(args, "effective_on", None) or date.today().isoformat()

    if args.input_type == "rate-stage":
        row = {"input_type": "rate_stage", "effective_on": effective_on,
               "value_text": args.stage, "note": args.note}
    elif args.input_type == "memory-margin":
        row = {"input_type": "memory_margin", "effective_on": effective_on,
               "value_num": args.value, "note": args.note}
    elif args.input_type == "pbr":
        row = {"input_type": "pbr", "effective_on": effective_on,
               "value_num": args.value, "note": args.note}
    else:  # event
        row = {"input_type": "event", "effective_on": effective_on, "note": args.note}

    store.insert_manual_input(row)
    print(f"기록됨 [{args.input_type}] effective_on={effective_on} {row.get('value_text') or row.get('value_num') or row.get('note')}")


if __name__ == "__main__":
    main()
