"""
네 개의 개별 수집기(us_market·kr_market·global_markets·news)를 순서대로
묶어 실행하는 진입점.

지금까지는 아래 네 줄을 따로 돌려야 했다:
    python collectors/us_market.py 1y
    python collectors/kr_market.py 1y
    python collectors/global_markets.py 1y
    python collectors/news.py
이 스크립트 하나로 순서대로(디버깅하기 쉬우라고 병렬이 아니라 순차) 실행한다.
GitHub Actions로 매일 자동 실행하는 걸 감안한 설계다:

  - 수집기 하나가 예외를 던져도 나머지는 계속 실행된다(서로 독립) — 예를 들어
    뉴스 쪽이 막혀 있어도 시세 수집은 끝까지 돼야 한다.
  - 이 스크립트가 각 수집기 실행마다 db.store.log_run()으로 collect_log에
    성공/실패를 남긴다. (각 collect_all()도 내부적으로 종목 단위 로그를 이미
    남기지만, 그건 종목별 세부 기록이고 이건 "이 배치 실행 자체가 됐는가"를
    job 단위로 남기는 별도 기록이다 — collect_log는 원래 누적 로그가 목적이라
    이런 중복 기록은 원칙 6 위반이 아니다. daily_price/news_items 쪽은 여전히
    각 collect_all()의 upsert_* 호출이 멱등하게 처리한다.)
  - 하나라도 실패하면 프로세스 종료 코드를 0이 아니게 반환해서 GitHub
    Actions가 실패를 감지할 수 있게 한다. 부분 실패/전체 실패는 구분하지
    않는다 — "하나라도 실패=비정상 종료"면 충분하다.
  - `--only`로 일부 job만 선택 실행할 수 있다(디버깅·재시도용).
  - `--dry-run`으로 실제 수집 없이 어떤 job이 실행될지만 미리 볼 수 있다
    (DB에 쓰지 않음, 네트워크 호출 없음).

사용 예:
    python collectors/collect_daily.py
    python collectors/collect_daily.py --period 6mo
    python collectors/collect_daily.py --only kr,global,news
    python collectors/collect_daily.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors import dart_client, global_markets, kr_market, news, us_market  # noqa: E402
from db import store  # noqa: E402

# (job 결과) job 이름, 실행 결과(성공/실패/None=dry-run으로 건너뜀), rows, 에러메시지
JobResult = tuple[str, bool | None, int, str | None]

# --only에서 쓰는 짧은 이름 -> (collect_log에 남는 job 이름, 전체 수집기 이름)
# 순서가 곧 실행 순서다. **"dart"는 일부러 JOB_ORDER(기본 실행 목록)에
# 안 넣었다** — DART_API_KEY를 아직 검증하지 못한 상태라(이 세션은 키 없이
# 구현), 실제 키로 한 번도 호출해본 적 없는 코드를 매일 자동 배치에 바로
# 태우는 건 더 신중해야 한다고 판단했다(collectors/kis_client.py의
# collect_investor_flow()를 아직 안 묶은 것과 같은 이유). `--only dart`로
# 명시해야만 실행된다.
JOB_ORDER = ["us", "kr", "global", "news"]
JOB_LOG_NAMES = {
    "us": "us_market", "kr": "kr_market", "global": "global_markets",
    "news": "news", "dart": "dart",
}
ALL_JOB_KEYS = JOB_LOG_NAMES  # --only가 허용하는 전체 키(기본 실행 목록보다 넓음)


def _job_functions(period: str) -> dict[str, Callable[[], int]]:
    """짧은 이름 -> 실제로 실행할 함수. period는 시세 수집기(us/kr/global)에만 쓰인다
    (news.collect_all()·dart_client.collect_all()은 기간 인자가 없음 —
    dart_client는 DART_API_KEY 없으면 네트워크를 전혀 시도하지 않고 0을 반환한다)."""
    return {
        "us": lambda: us_market.collect_all(period=period),
        "kr": lambda: kr_market.collect_all(period=period),
        "global": lambda: global_markets.collect_all(period=period),
        "news": lambda: news.collect_all(),
        "dart": lambda: dart_client.collect_all(),
    }


def _run_job(job: str, fn: Callable[[], int]) -> JobResult:
    """
    fn()을 실행하고 성공/실패와 무관하게 collect_log에 기록한다.
    fn이 예외를 던지면 여기서 잡아서 다음 job으로 넘어갈 수 있게 한다.
    """
    try:
        rows = fn()
        store.log_run(job, "ok", rows, "")
        return job, True, rows, None
    except Exception as e:  # 한 수집기 실패가 나머지를 막지 않게
        store.log_run(job, "fail", 0, str(e))
        return job, False, 0, str(e)


def collect_daily(
    period: str = "1y",
    only: list[str] | None = None,
    dry_run: bool = False,
) -> list[JobResult]:
    """
    선택된 수집기를 순차 실행하고 (job, 성공여부, rows, 에러메시지) 목록을 반환한다.

    only: ["kr", "global", "news"]처럼 짧은 이름 목록. None이면 전체(JOB_ORDER) 실행.
    dry_run: True면 아무것도 실행/기록하지 않고 (job, None, 0, None)만 돌려준다
             (어떤 job이 실행될지 미리보기용 — DB 접근도 안 함).
    """
    keys = only if only is not None else list(JOB_ORDER)
    unknown = [k for k in keys if k not in JOB_LOG_NAMES]
    if unknown:
        raise ValueError(
            f"알 수 없는 job: {', '.join(unknown)} (가능한 값: {', '.join(JOB_ORDER)})"
        )

    if dry_run:
        return [(JOB_LOG_NAMES[k], None, 0, None) for k in keys]

    store.init_db()
    funcs = _job_functions(period)
    return [_run_job(JOB_LOG_NAMES[k], funcs[k]) for k in keys]


def main() -> None:
    p = argparse.ArgumentParser(description="us_market·kr_market·global_markets·news 수집을 순서대로 실행")
    p.add_argument("--period", default="1y", help="us_market/kr_market/global_markets에 전달할 수집 기간 (기본 1y)")
    p.add_argument(
        "--only", default=None,
        help=f"실행할 job만 쉼표로 지정 (예: kr,global,news 또는 dart). "
             f"가능한 값: {','.join(ALL_JOB_KEYS)}. 기본(전체)은 {','.join(JOB_ORDER)} — "
             f"dart는 DART_API_KEY 검증 전이라 --only로 명시해야만 실행됨",
    )
    p.add_argument("--dry-run", action="store_true", help="실제로 수집하지 않고 어떤 job이 실행될지만 보여준다")
    args = p.parse_args()

    only = [s.strip() for s in args.only.split(",")] if args.only else None

    try:
        results = collect_daily(args.period, only=only, dry_run=args.dry_run)
    except ValueError as e:
        p.error(str(e))
        return

    if args.dry_run:
        print(f"[dry-run] period={args.period} — 다음 job을 이 순서로 실행합니다(실제 수집은 안 함):")
        for job, _, _, _ in results:
            print(f"  - {job}")
        return

    print(f"[collect_daily] period={args.period} only={only or 'all'}")
    print("\n=== 수집 요약 ===")
    any_failed = False
    for job, ok, rows, err in results:
        if ok:
            print(f"  {job:15s} 성공 · {rows}행")
        else:
            print(f"  {job:15s} 실패 · {err}")
            any_failed = True

    if any_failed:
        print("\n일부 수집기가 실패했습니다. collect_log 테이블에서 자세한 사유를 확인하세요.")
        sys.exit(1)


if __name__ == "__main__":
    main()
