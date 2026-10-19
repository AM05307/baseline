"""
에이전트 배치 실행 CLI. Anthropic API를 호출하는 유일한 진입점이다.

원칙: 대시보드를 열거나(web/main.py) MCP 툴을 호출하는 것만으로는 절대
API 비용이 발생하지 않는다. 이 스크립트를 사람이 직접 실행할 때만 호출되고,
결과는 DB에 캐싱돼서 대시보드/MCP는 그 캐시만 읽는다("배치로 미리 돌려서
캐싱" — 세션에서 사용자가 고른 방식).

사용 전 준비:
    1. .env에 ANTHROPIC_API_KEY 설정 (.env.example 참고)
    2. pip install anthropic  (requirements.txt에 포함됨)

사용 예:
    # 실제로 API를 부르기 전에 무엇이 전송되는지 먼저 확인 (비용 없음, 추천)
    python scripts/run_agents.py --ticker 000660 --dry-run

    # 실제 실행 (비용 발생)
    python scripts/run_agents.py --ticker 000660

    python scripts/run_agents.py --ticker 000660 --model claude-opus-4-1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402,F401  (import 자체가 .env를 os.environ으로 로드함)
from agents import orchestrator  # noqa: E402
from db import store  # noqa: E402


def _print_dry_run(ticker: str, today: date) -> None:
    print("=== dry-run: 실제 API 호출 없음, 아래 내용이 전송될 예정입니다 ===\n")
    print("[system]")
    print(orchestrator.SYSTEM_PROMPT)
    print("\n[user]")
    print(orchestrator.USER_PROMPT_TEMPLATE.format(ticker=ticker, today=today.isoformat()))
    print(f"\n[tools] {len(orchestrator.TOOL_SPECS)}개: "
          + ", ".join(t["name"] for t in orchestrator.TOOL_SPECS))
    print(f"\n[model 기본값] {orchestrator.DEFAULT_MODEL}")
    print("\n실제로 돌리려면 --dry-run 없이 다시 실행하세요. API 비용이 발생합니다.")


def main() -> None:
    p = argparse.ArgumentParser(description="에이전트 배치 분석 실행 (Anthropic API 호출)")
    p.add_argument("--ticker", default=orchestrator.HYNIX, help="분석 대상 종목 (기본 000660)")
    p.add_argument("--model", default=orchestrator.DEFAULT_MODEL)
    p.add_argument("--dry-run", action="store_true", help="API를 부르지 않고 전송될 내용만 출력")
    args = p.parse_args()

    today = date.today()

    if args.dry_run:
        _print_dry_run(args.ticker, today)
        return

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY가 설정돼 있지 않습니다. .env에 추가하세요 (.env.example 참고).")
        print("먼저 --dry-run으로 전송될 내용을 확인해볼 수 있습니다.")
        sys.exit(1)

    try:
        from anthropic import Anthropic
    except ImportError:
        print("anthropic 패키지가 설치돼 있지 않습니다: pip install anthropic")
        sys.exit(1)

    store.init_db()
    client = Anthropic(api_key=api_key)

    print(f"{args.ticker} 분석 중... (모델: {args.model}, API 호출 발생)")
    result = orchestrator.analyze_ticker(client, ticker=args.ticker, model=args.model, today=today)

    print(f"\n툴 호출 {len(result.tool_calls)}회, 반복 {result.iterations}회, "
          f"토큰 입력 {result.input_tokens}/출력 {result.output_tokens}")

    if result.warnings:
        print("\n⚠ 경고 발견 — DB에 저장하지 않았습니다. 아래 내용을 직접 검토하세요:")
        for w in result.warnings:
            print(f"  - {w}")
        print("\n--- 생성된 텍스트 (검토용, 미저장) ---")
        print(result.narrative)
        sys.exit(2)

    store.upsert_agent_analysis({
        "ticker": result.ticker,
        "run_date": today.isoformat(),
        "model": result.model,
        "narrative": result.narrative,
        "tool_calls": json.dumps(result.tool_calls, ensure_ascii=False, default=str),
        "warnings": json.dumps(result.warnings, ensure_ascii=False),
    })

    print(f"\n저장됨 [{result.ticker} · {today.isoformat()}]\n")
    print(result.narrative)


if __name__ == "__main__":
    main()
