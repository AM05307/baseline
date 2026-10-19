"""
킬 스위치 — 켜면 모든 주문과 섀도 실행이 즉시 멈춘다(섀도 기록에는 "킬 스위치로 차단됨"으로 남음).

    python -m executor.scripts.kill_switch on --reason "점검"
    python -m executor.scripts.kill_switch off --reason "점검 끝"
    python -m executor.scripts.kill_switch status
가장 빠른 방법은 파일: 저장소 루트에서 `touch KILL_SWITCH`(끄려면 파일 삭제). 파일·DB 중 하나만 켜져도 멈춘다.
"""

from __future__ import annotations

import argparse
import sys

from executor import config_files, kill_switch, store


def main(argv: list[str] | None = None, db_path=None, file_path=None) -> int:
    p = argparse.ArgumentParser(description="킬 스위치")
    p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--reason", default="")
    args = p.parse_args(argv)
    store.init_db(db_path)
    if file_path is None:
        file_path = config_files.resolve(config_files.load_executor_config(db_path=db_path)["kill_switch_file"])
    if args.action in ("on", "off"):
        if not args.reason:
            p.error("--reason 필요(기록으로 남음)")
        store.set_kill_switch(args.action == "on", args.reason, path=db_path)
    state = kill_switch.engaged(file_path, db_path)
    print(f"킬 스위치: {'켜짐 — ' + state if state else '꺼짐'}")
    if args.action == "off" and state:
        print("  (DB는 껐지만 파일 플래그가 남아 있습니다 — 파일을 지워야 풀립니다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
