"""
정지 해제 — 자동 정지된 계좌의 주문을 사람이 원인을 확인한 뒤 다시 허용한다.
  - halt(잔고 불일치): 원장·브로커 차이를 직접 정리한 뒤 해제
  - review_required(MDD·괴리·주문 오류로 강등): "정리할지"를 사람이 정한다. 해제하면 다음 실행부터 줄어든 운용 자본에
    맞춰 주문(=정리)하고, 보유를 유지하려면 해제하지 않는다.
단계는 바꾸지 않는다(강등된 단계는 그대로, 재승격은 promote_tier로).

사용:
    python -m executor.scripts.resume --account real --reason "수동 매수분 확인, 원장 정정 완료"
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from executor import store


def main(argv: list[str] | None = None, db_path=None) -> int:
    p = argparse.ArgumentParser(description="정지(halt) 해제")
    p.add_argument("--account", required=True, choices=["shadow", "paper", "real"])
    p.add_argument("--reason", required=True, help="원인과 조치(기록으로 남음)")
    args = p.parse_args(argv)
    store.init_db(db_path)
    state = store.freeze_state(args.account, db_path)
    if not state:
        print(f"{args.account} 계좌는 정지 상태가 아닙니다.")
        return 1
    store.append_event(date.today().isoformat(), args.account, "resume", args.reason, path=db_path)
    print(f"{args.account} 계좌 정지({state}) 해제 기록.")
    print("주의: 다음 실행부터 현재 단계의 운용 자본에 맞춰 주문합니다 — 강등으로 자본이 줄었다면 보유분 일부 또는 전부를 "
          "매도하는 주문이 나갈 수 있습니다. 보유를 유지하려면 해제하지 마세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
