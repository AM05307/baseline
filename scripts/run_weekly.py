"""
매주 1회 실행을 염두에 둔 배치 체이닝: collect_daily -> compute_signals ->
recompute_weights를 이 순서로 실행한다(3단계 요청: "수집 진입점 통합").

**왜 import해서 로직을 합치지 않고 하위 프로세스로 부르나**: 세 스크립트는
각자 이미 완성된 독립 CLI다(각자 `store.init_db()`·인자 처리·종료 코드를
갖고 있다). 이 프로젝트의 기존 관행도 "CLI 스크립트는 얇은 배선, 테스트는
그 아래 로직만"이다(예: `scripts/add_claim.py`, `tests/test_grade_claims.py`
상단 설명 참고). 세 스크립트의 내부 로직을 가져와 다시 조립하면 중복이
생기고, 각 스크립트가 **단독으로도 계속 똑같이 동작해야 한다**는 요구와도
안 맞는다 — 그래서 `collectors/collect_daily.py`·`scripts/compute_signals.py`·
`scripts/recompute_weights.py`를 그대로 별도 프로세스로, 순서대로 호출한다.

**실패 처리가 `collect_daily.py` 내부와 다른 이유**: `collect_daily.py`는
"수집기 하나 실패해도 나머지는 계속"(서로 독립적인 데이터 소스라서)이지만,
이 체인의 세 **단계**는 서로 종속적이다 — compute_signals는 collect_daily가
채운 시세를 읽고, recompute_weights는 compute_signals가 채운 signal_values를
읽는다. 그래서 한 단계가 실패하면(종료 코드 ≠0) 거기서 **체인을 중단**하고
그 단계의 종료 코드를 그대로 돌려준다 — 부실한 입력으로 다음 단계를
계속 돌리는 게 더 위험하다고 판단했다.

`--dry-run`은 아무 서브프로세스도 실행하지 않고 실행될 명령만 보여준다
(collect_daily.py 자체의 `--dry-run`과는 레벨이 다르다 — 여긴 이 체인이
그 아래 세 스크립트를 "부를 것"이라는 계획만 보여준다. 실제로 collect_daily
단계만 네트워크를 타므로, 네트워크 비용 없이 전체 계획을 보고 싶으면
`--dry-run`을, collect_daily만 건너뛰고 나머지는 기존 DB로 실행하고
싶으면 `--only ...`로 `collect_daily.py`의 `--only`를 그대로 전달한다).

Windows 작업 스케줄러에 매주 실행으로 등록하는 방법은 README.md
"스케줄 등록" 섹션에 예시로 문서화돼 있다 — **이 스크립트는 등록 자체를
수행하지 않는다**(공통 규칙: 실제 배포/자동화 등록은 하지 않는다).

사용:
    python scripts/run_weekly.py
    python scripts/run_weekly.py --period 1y
    python scripts/run_weekly.py --only kr,global,news
    python scripts/run_weekly.py --min-events 20
    python scripts/run_weekly.py --dry-run
    python scripts/run_weekly.py --only memos          # 막스 메모 수집기만(collectors/marks_memos.py)
    python scripts/run_weekly.py --only kr,memos       # collect_daily --only kr 체인 + 끝에 메모 수집

`--only`의 `memos`는 collect_daily로 넘기지 않고 따로 떼어 메모 수집 단계로 쓴다. 메모 수집은 주 1회면 충분하고
(새 메모는 드물다) 체인의 다른 단계와 독립이라 맨 끝에 둔다. 기본 실행(`--only` 없음)에는 포함하지 않는다 —
이용약관 확인 표시(config/marks_memos.yaml)가 필요한 수집기라 명시적으로 고를 때만 돈다.
관점별 토론(scripts/run_debate.py)은 비용이 드는 API 호출이라 이 체인에 넣지 않는다(수동 실행만).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STEP_NAMES = ["collect_daily", "compute_signals", "recompute_weights"]


def _script_path(name: str) -> Path:
    if name == "collect_daily":
        return ROOT / "collectors" / "collect_daily.py"
    return ROOT / "scripts" / f"{name}.py"


def build_commands(period: str, only: str | None, min_events: int) -> list[tuple[str, list[str]]]:
    """(단계 이름, 실행할 명령 argv) 목록을 순서대로 만든다. 실행은 안 함 — 순수 함수라 테스트하기 쉽다."""
    tokens = [t.strip() for t in (only or "").split(",") if t.strip()]
    memos = "memos" in tokens
    rest = [t for t in tokens if t != "memos"]
    memos_cmd = ("marks_memos", [sys.executable, str(ROOT / "collectors" / "marks_memos.py")])
    if memos and not rest:
        return [memos_cmd]
    collect_cmd = [sys.executable, str(_script_path("collect_daily")), "--period", period]
    if rest:
        collect_cmd += ["--only", ",".join(rest)]
    compute_cmd = [sys.executable, str(_script_path("compute_signals"))]
    weights_cmd = [sys.executable, str(_script_path("recompute_weights")), "--min-events", str(min_events)]
    commands = [
        ("collect_daily", collect_cmd),
        ("compute_signals", compute_cmd),
        ("recompute_weights", weights_cmd),
    ]
    return commands + [memos_cmd] if memos else commands


def main() -> None:
    p = argparse.ArgumentParser(
        description="매주 1회 실행용 체인: collect_daily -> compute_signals -> recompute_weights"
    )
    p.add_argument("--period", default="1y", help="collect_daily에 전달할 수집 기간 (기본 1y)")
    p.add_argument("--only", default=None, help="collect_daily --only에 그대로 전달 (예: kr,global,news)")
    p.add_argument("--min-events", type=int, default=30, help="recompute_weights --min-events에 전달 (기본 30)")
    p.add_argument("--dry-run", action="store_true", help="아무 서브프로세스도 실행하지 않고 실행될 명령만 보여준다")
    args = p.parse_args()

    commands = build_commands(args.period, args.only, args.min_events)

    if args.dry_run:
        print("[run_weekly] --dry-run: 아래 명령을 이 순서로 실행합니다 (실제 실행 안 함):")
        for name, cmd in commands:
            print(f"  [{name}] {' '.join(cmd)}")
        return

    for name, cmd in commands:
        print(f"\n=== [run_weekly] {name} 시작 ===")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"\n[run_weekly] {name} 단계가 실패했습니다 (종료 코드 {result.returncode}) — 체인을 중단합니다.")
            sys.exit(result.returncode)

    print("\n[run_weekly] 전체 완료: collect_daily -> compute_signals -> recompute_weights")


if __name__ == "__main__":
    main()
