"""
데모: 수집 → 저장 → 분석 전체 흐름 확인.

한투 API 키 없이도 미국 데이터만으로 파이프라인이 도는지 검증한다.
먼저 `python collectors/us_market.py` 를 한 번 실행할 것.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import calendar_rules as cal
from analysis import correlation as corr
from config import settings
from db import store


def section(title: str) -> None:
    print(f"\n{'=' * 52}\n{title}\n{'=' * 52}")


def main() -> None:
    section("오늘의 캘린더")
    evs = cal.events_for(date.today())
    if evs:
        for e in evs:
            print(f"  [{e.kind}] {e.label}\n      {e.note}")
    else:
        print("  해당 이벤트 없음")

    print("\n  다가오는 일정:")
    for e in cal.upcoming(date.today(), 45)[:6]:
        print(f"    {e.date}  {e.label}")

    section("종목 간 관계 측정")
    store.init_db()  # 테이블 없으면 생성. 이미 있으면 아무 일 없음
    sox = store.get_prices("SOX")
    if sox.empty:
        print("  데이터 없음. 먼저 실행:")
        print("    python collectors/us_market.py 1y")
        return

    for name in ("NVDA", "MU", "AMD", "TSM"):
        s = store.get_prices(name)
        if s.empty:
            continue
        print(f"\n  [{name}] vs SOX")
        for line in corr.summarize(s, sox, window=60).splitlines():
            print(f"  {line}")

    section("평소 관계에서 벗어난 날")
    nvda = store.get_prices("NVDA")
    if not nvda.empty:
        d = corr.divergence(nvda, sox, window=120, threshold=2.0)
        if d.empty:
            print("  없음")
        else:
            print("  (개별 재료가 있었을 가능성이 있는 날)")
            for idx, r in d.head(5).iterrows():
                print(f"    {idx.date()}  NVDA {r.target*100:+.2f}% / "
                      f"SOX {r.benchmark*100:+.2f}%  (z={r.z:+.1f})")

    section("환경 상태")
    print(f"  KIS API 키: {'설정됨' if settings.kis_ready else '미설정 (미국 데이터만 사용 가능)'}")
    logs = store.recent_logs(3)
    if not logs.empty:
        print("  최근 수집 로그:")
        for _, r in logs.iterrows():
            print(f"    {r.ran_at}  {r.job:12s} {r.status:4s} {r.rows} rows")

    print("\n※ 위는 관측값입니다. 향후 주가 방향을 예측하지 않습니다.\n")


if __name__ == "__main__":
    main()
