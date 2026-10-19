"""
킬 스위치 — 파일 플래그 **또는** DB 값 중 하나만 켜져도 모든 주문(섀도 기록 포함)을 즉시 막는다.

  - 파일: config/executor.yaml의 kill_switch_file(기본 저장소 루트 KILL_SWITCH). 파일이 있으면 켜짐(내용 무관).
    터미널에서 `touch KILL_SWITCH` 한 줄로 켤 수 있게 일부러 단순하게 했다.
  - DB: executor.db kill_switch_events의 마지막 행이 'on'. `python -m executor.scripts.kill_switch on --reason ...`.
엔진은 실행 시작과 **주문 하나하나를 보내기 직전**에 확인한다(실행 도중 켜도 남은 주문은 안 나간다).
"""

from __future__ import annotations

from pathlib import Path

from executor import store

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "KILL_SWITCH"


def engaged(file_path: Path | None = DEFAULT_FILE, db_path: Path | None = None) -> str | None:
    """켜져 있으면 사유 문자열, 아니면 None."""
    if file_path is not None and Path(file_path).exists():
        return f"킬 스위치 파일 {Path(file_path).name} 있음"
    row = store.kill_switch_db(db_path)
    if row:
        return f"킬 스위치 DB 켜짐({row['at']}): {row['reason']}"
    return None
