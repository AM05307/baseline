"""
재시작 복구 — 엔진이 주문을 내기 전에 한 번, 결과가 확정되지 않은 주문('sending'·'submitted')을 브로커 조회로 맞춘다.

참고: freqtrade `startup_update_open_orders`, Lean `LoadExistingHoldingsAndOrders`, Nautilus 시작 대사(설계만 참고,
docs/reference_gap_analysis.md R-2). 브로커가 기준이다.

자동으로 처리하는 것(확실한 경우만):
  - 브로커가 **끝난 주문**으로 보고한다(전량 체결 / 잔량 취소 확인 / 거부) → 원장에 아직 없는 체결 수량만 기록하고 상태 확정.
  - 섀도(ledger_is_broker): 원장에 체결 흔적이 없으면 실행되지 않은 것 → 'not_sent'.
  - 브로커 주문번호가 없는 'sending' 행(전송 직후 종료): 같은 종목·방향이고 수량이 계획 이하인, 아직 짝이 없는 브로커 주문이
    **정확히 하나**이고 끝난 주문이면 그 주문으로 확정한다. 이후 엔진의 잔고 대사가 이 짝짓기를 다시 검증한다.
사람이 판단해야 하는 것(정지 + 사유 기록):
  - 브로커에 아직 살아 있는 잔량이 있다(취소할지·기다릴지는 사람이 정한다). 이미 확인된 체결분은 원장에 먼저 기록한다.
  - 짝이 되는 브로커 주문이 없거나 둘 이상이다.
  - 브로커 체결 수량이 원장 기록보다 적다(앞뒤가 안 맞음).
  - 브로커 조회 자체가 실패했다.
이 모듈은 주문을 보내지도 취소하지도 않는다(조회 + 원장 기록만).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from executor import store
from executor.broker.base import Broker, BrokerError, BrokerOrder


@dataclass
class RecoveryResult:
    resolved: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)

    @property
    def needs_manual(self) -> bool:
        return bool(self.manual)


def _same_id(a: str | None, b: str) -> bool:
    """브로커 주문번호 비교 — KIS는 '지점:주문번호'이고 조회 응답의 지점 표기가 다를 수 있어 주문번호만 본다."""
    if not a:
        return False
    return a.split(":")[-1].lstrip("0") == b.split(":")[-1].lstrip("0")


def _record_fill_delta(row: dict, filled: int, avg: float | None, date: str, account: str, commission: float,
                       tax: float, path: Path | None) -> int | None:
    """원장에 아직 없는 체결분만 기록. 반환: 새로 기록한 수량, 앞뒤가 안 맞으면 None."""
    recorded = store.ledger_qty_for_order(row["order_key"], path)
    delta = filled - recorded
    if delta < 0:
        return None
    if delta > 0:
        price = avg or float(row["ref_price"])
        gross = delta * price
        if row["side"] == "buy":
            store.append_ledger(date, account, "buy", -gross * (1 + commission), row["ticker"], delta, price,
                                row["order_key"], note="재시작 복구", path=path)
        else:
            store.append_ledger(date, account, "sell", gross * (1 - commission - tax), row["ticker"], -delta, price,
                                row["order_key"], note="재시작 복구", path=path)
    return delta


def _final_status(b: BrokerOrder) -> str:
    if b.status == "rejected":
        return "rejected"
    if b.filled_qty >= b.qty > 0:
        return "filled"
    return "partial" if b.filled_qty > 0 else "cancelled"


def recover_on_start(broker: Broker, account: str, date: str, *, commission: float, tax: float,
                     path: Path | None = None) -> RecoveryResult:
    res = RecoveryResult()
    rows = store.in_flight_orders(account, path)
    if not rows:
        return res
    touched_decisions: dict[str, bool] = {}  # decision_date → 복구한 주문이 전부 전량 체결이었나

    if broker.ledger_is_broker:
        for r in rows:
            done = store.ledger_qty_for_order(r["order_key"], path)
            status = "not_sent" if done == 0 else ("filled" if done >= int(r["qty"]) else "partial")
            store.update_order(r["order_key"], status=status, filled_qty=done, path=path)
            res.resolved.append(f"{r['order_key']}: 원장 기준 {status}({done}주)")
            touched_decisions[r["decision_date"]] = touched_decisions.get(r["decision_date"], True) and status == "filled"
    else:
        try:
            border = broker.list_orders(min(r["decision_date"] for r in rows))
        except BrokerError as e:
            res.manual.append(f"브로커 주문 조회 실패 — 미확정 주문 {len(rows)}건을 확인할 수 없음: {e}")
            return res
        linked = store.linked_broker_ids(account, path)
        for r in rows:
            key = r["order_key"]
            if r["broker_order_id"]:
                match = [b for b in border if _same_id(r["broker_order_id"], b.broker_order_id)]
                how = "주문번호"
            else:
                match = [b for b in border if b.ticker == r["ticker"] and b.side == r["side"] and b.qty <= int(r["qty"])
                         and not any(_same_id(x, b.broker_order_id) for x in linked)]
                how = "종목·방향·수량"
            if len(match) != 1:
                res.manual.append(f"{key}: 짝이 되는 브로커 주문이 {len(match)}건({how} 기준) — 전송 여부를 사람이 확인")
                continue
            b = match[0]
            if not r["broker_order_id"]:
                store.update_order(key, broker_order_id=b.broker_order_id, path=path)
                linked.add(b.broker_order_id)
            delta = _record_fill_delta(r, b.filled_qty, b.avg_price, date, account, commission, tax, path)
            if delta is None:
                res.manual.append(f"{key}: 브로커 체결 {b.filled_qty}주 < 원장 기록 — 앞뒤가 맞지 않음")
                continue
            if b.open_qty > 0:
                store.update_order(key, status="submitted", filled_qty=b.filled_qty, path=path)
                res.manual.append(f"{key}: 브로커에 미체결 잔량 {b.open_qty}주(체결 {b.filled_qty}/{b.qty}주는 원장에 반영) — "
                                  "취소할지 사람이 판단")
                touched_decisions[r["decision_date"]] = False
                continue
            status = _final_status(b)
            store.update_order(key, status=status, filled_qty=b.filled_qty, path=path)
            res.resolved.append(f"{key}: {how}로 확인 — {status}({b.filled_qty}/{b.qty}주, 원장 +{delta}주)")
            touched_decisions[r["decision_date"]] = touched_decisions.get(r["decision_date"], True) and status == "filled"

    # 대기 중 결정에 속한 주문이 전량 체결이 아니면 그 결정은 더 집행하지 않는다(남은 주문은 다음 결정이 다시 계산).
    for d, all_filled in touched_decisions.items():
        if not all_filled:
            store.set_decision_status(d, account, "incomplete", path)
    for m in res.resolved:
        store.append_event(date, account, "recovery_auto", m, path=path)
    for m in res.manual:
        store.append_event(date, account, "recovery_manual", m, path=path)
    return res
