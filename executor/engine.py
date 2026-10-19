"""
하루 한 번 실행 흐름(계좌 하나):
  킬 스위치 → 정지 상태 → 재시작 복구(미확정 주문을 브로커 조회로 확정, 애매하면 정지) → 잔고 대사(전) → 임시 스냅샷·강등 검사 → 운용 자본 → 주문 계획 → 한도 검사 →
  주문(하나씩 보내기 직전 킬 스위치 재확인) → 체결 → 원장 기록 → 잔고 대사(후) → 강등 검사 → 스냅샷.

강등되면 그 계좌는 정지된다(ladder_ops.demote): 잔고 불일치면 'halt'(모든 주문 정지), MDD·괴리·주문 오류면
'review_required'(신규 주문 정지·보유 유지·수동 판단). 강등된 실행에서는 주문을 보내지 않는다.
한도 초과로 거부된 계획(cap_rejected)은 1회는 강등 사유가 아니고, 연속 cap_rejected_streak회마다 주문 오류 1건으로 센다.

목표 수량은 analysis/position_policy(백테스트와 같은 함수)로 정한다. 기본 정책은 "목표 비중이 바뀔 때만 다시 계산"이라
직전에 체결까지 끝낸 목표(target_history)를 읽어서 넘기고, 이번 계획이 전부 체결되면(또는 주문이 필요 없었으면) 기록한다.

이 모듈은 브로커를 인자로 받는다(테스트·섀도는 FakeBroker 계열). 실제 자금 계좌용 브로커 구현체는 없다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from analysis.position_policy import PositionPolicy
from executor import kill_switch, ladder_ops, recovery, store
from executor.broker.base import Broker, BrokerError
from executor.planner import (DailyLimits, DailyState, apply_daily_limits, cap_single_weights, check_plan_within_cap,
                              plan_orders)
from executor.tiers import Ladder, check_demotion, operating_capital

DEFAULT_COMMISSION = 0.00015  # analysis/trading_costs.DEFAULT_COMMISSION_RATE와 같은 값(참고용 근사치)
DEFAULT_TAX = 0.002  # analysis/trading_costs 기본 매도세(참고용 근사치)
DEFAULT_CAP_REJECTED_STREAK = 3
FILL_MODES = ("next_open", "same_close")
DEFAULT_FILL = "next_open"  # analysis/what_if.DEFAULT_FILL과 같은 값(백테스트와 같은 체결 시점)
MAX_POLLS = 3


@dataclass
class RunResult:
    status: str  # 'ok' | 'blocked_kill_switch' | 'halt' | 'review_required' | 'cap_rejected' | 'mismatch' | 'api_error'
    tier: str
    operating_capital: float = 0.0  # 실행이 끝난 뒤(체결·수수료 반영) 기준
    planning_capital: float = 0.0  # 이번 주문 계획에 실제로 쓴 자본 = min(운용분, 한도)
    sleeve_value: float = 0.0
    account_value: float | None = None
    orders_sent: int = 0
    orders_skipped_duplicate: int = 0
    demoted_to: list[str] = field(default_factory=list)
    planned: list = field(default_factory=list)  # 이번 실행의 주문 계획(PlannedOrder) — 섀도 기록용
    warnings: list = field(default_factory=list)  # 주문을 막지 않은 경고(주문 단위로 목표 비중 미달 등)
    message: str = ""


def _mismatch(broker_pos: dict[str, int], internal: dict[str, int]) -> str | None:
    keys = set(broker_pos) | set(internal)
    diff = {k: (broker_pos.get(k, 0), internal.get(k, 0)) for k in keys if broker_pos.get(k, 0) != internal.get(k, 0)}
    return None if not diff else "브로커/내부 수량 불일치 " + ", ".join(f"{k}: {b}/{i}" for k, (b, i) in sorted(diff.items()))


def _auto_demote(ladder: Ladder, date: str, account: str, benchmark_close, path, result: RunResult) -> bool:
    """현재 단계 계좌라면 강등 판정. 강등(또는 맨 아래 단계 정지)했으면 True."""
    cur, m = ladder_ops.current_metrics(ladder, benchmark_close, path)
    tier = ladder.get(cur["to_tier"])
    if tier.account != account:  # 이 계좌가 현재 단계의 계좌가 아니면 그 단계의 강등 판정 대상이 아니다
        return False
    reasons = check_demotion(tier.demotion, m)
    if not reasons:
        return False
    new = ladder_ops.demote(ladder, date, reasons, account, path)
    if new:
        result.demoted_to.append(new)
    return True


def _finish(result: RunResult, status: str, date: str, account: str, path, message: str = "") -> RunResult:
    result.status = status
    if message:
        result.message = message
    store.append_engine_run(date, account, status, result.message, path=path)
    return result


def _handle_mismatch(ladder, date, account, problem, when, benchmark_close, path, result) -> RunResult:
    store.append_event(date, account, "reconcile_mismatch", f"{when}: {problem}", path=path)
    _auto_demote(ladder, date, account, benchmark_close, path, result)
    if store.freeze_state(account, path) != "halt":  # 강등 규칙이 없거나 현재 단계 계좌가 아니어도 불일치면 멈춘다
        store.append_event(date, account, "halt", "잔고 불일치 — 모든 주문 정지(정리 주문 포함)", path=path)
    return _finish(result, "mismatch", date, account, path, problem)


def _recover(broker: Broker, date: str, account: str, commission: float, tax: float, path, result: RunResult) -> bool:
    """재시작 복구(executor/recovery.py). 사람이 판단해야 할 것이 있으면 정지(halt)하고 True."""
    rec = recovery.recover_on_start(broker, account, date, commission=commission, tax=tax, path=path)
    if rec.resolved:
        result.message = f"재시작 복구: 미확정 주문 {len(rec.resolved)}건 확정"
    if rec.needs_manual:
        store.append_event(date, account, "halt", "재시작 복구 — 사람이 판단할 주문: " + " / ".join(rec.manual), path=path)
        result.message = "재시작 복구에서 수동 판단 필요: " + " / ".join(rec.manual)
        return True
    return False


def _daily_state(account: str, date: str, sleeve: float, new_keys: set[str], path) -> DailyState:
    """일일 제한 판정 입력 — 전부 executor.db에서 읽는다(재시작해도 유지)."""
    snaps = store.snapshots(account, path=path)
    prev = snaps[(snaps["date"] < date) & snaps["sleeve_value"].notna()] if not snaps.empty else snaps
    change = None
    if not prev.empty and float(prev["sleeve_value"].iloc[-1]) > 0:
        change = (sleeve / float(prev["sleeve_value"].iloc[-1]) - 1) * 100
    od = store.orders(account, path)
    today = 0 if od.empty else int(((od["decision_date"] == date) & ~od["order_key"].isin(new_keys)).sum())
    ev = store.events(account, path=path)
    frz = ev[ev["kind"].isin(store.FREEZE_KINDS)] if not ev.empty else ev
    days = None
    if not frz.empty:
        from datetime import date as _d
        days = (_d.fromisoformat(date) - _d.fromisoformat(str(frz["date"].iloc[-1])[:10])).days
    return DailyState(change, today, days)


def run_once(broker: Broker, ladder: Ladder, decision_date: str, target_weights: dict[str, float],
             prices: dict[str, float], strategy: str = "risk_score", reference_value: float | None = None,
             benchmark_close=None, commission: float = DEFAULT_COMMISSION, tax: float = DEFAULT_TAX,
             path: Path | None = None, cap_rejected_streak: int = DEFAULT_CAP_REJECTED_STREAK,
             kill_switch_file: Path | None = kill_switch.DEFAULT_FILE,
             policy: PositionPolicy | None = None, fill: str = DEFAULT_FILL,
             max_weight_gap_pctp: float = 10.0, daily_limits: DailyLimits | None = None,
             max_single_weight: float | None = None, weight_cap_exempt: frozenset[str] = frozenset()) -> RunResult:
    """
    daily_limits·max_single_weight(기본 None = 끔): 운영 제한(planner.apply_daily_limits·cap_single_weights).
    종목당 상한은 맨 처음 목표 비중에 적용한다 — 체결 완료 목표(target_history)도 자른 값으로 남아야 "목표가 바뀔 때만"
    규칙이 백테스트와 같게 돈다.
    fill="next_open"(기본, 2026-10-19 결정): 오늘 종가로 계획한 주문을 'planned'로 저장만 하고 브로커에는 보내지 않는다.
    다음 거래일 시가에 execute_pending()이 보낸다. fill="same_close": 계획 즉시 보낸다(이전 기본).
    """
    if fill not in FILL_MODES:
        raise ValueError(f"fill은 {FILL_MODES} 중 하나: {fill}")
    account = broker.account_kind
    ladder_ops.ensure_initialized(ladder, decision_date, path)
    result = RunResult(status="ok", tier=store.current_tier(path)["to_tier"])
    target_weights, cap_notes = cap_single_weights(target_weights, max_single_weight, weight_cap_exempt)

    ks = kill_switch.engaged(kill_switch_file, path)
    if ks:
        return _finish(result, "blocked_kill_switch", decision_date, account, path, ks)
    frozen = store.freeze_state(account, path)
    if frozen:
        return _finish(result, frozen, decision_date, account, path,
                       "정지 상태 — 사람이 원인 확인 후 executor.scripts.resume으로 해제해야 주문")
    if _recover(broker, decision_date, account, commission, tax, path, result):
        return _finish(result, "halt", decision_date, account, path)

    try:
        broker_pos = broker.get_positions()
    except BrokerError as e:
        store.append_event(decision_date, account, "api_error", str(e), path=path)
        return _finish(result, "api_error", decision_date, account, path, str(e))
    problem = _mismatch(broker_pos, store.internal_positions(account, path))
    if problem:
        return _handle_mismatch(ladder, decision_date, account, problem, "주문 전", benchmark_close, path, result)

    # 오늘 평가로 임시 스냅샷을 먼저 남기고 강등을 판정한다 — 오늘 MDD 초과가 오늘 주문에 바로 반영되게.
    tier = ladder.get(store.current_tier(path)["to_tier"])
    sleeve = ladder_ops.sleeve_value(account, prices, path)
    _snapshot(broker, decision_date, account, tier.name, sleeve,
              operating_capital(tier, account, sleeve, ladder), reference_value, path, result)
    if _auto_demote(ladder, decision_date, account, benchmark_close, path, result):
        tier = ladder.get(store.current_tier(path)["to_tier"])
        capital = operating_capital(tier, account, sleeve, ladder)
        result.tier, result.sleeve_value, result.operating_capital = tier.name, sleeve, capital
        _snapshot(broker, decision_date, account, tier.name, sleeve, capital, reference_value, path, result)
        return _finish(result, store.freeze_state(account, path) or "review_required", decision_date, account,
                       path, "강등 — 이번 실행은 주문하지 않음(보유 유지)")

    capital = operating_capital(tier, account, sleeve, ladder)
    result.tier, result.sleeve_value, result.operating_capital = tier.name, sleeve, capital
    result.planning_capital = capital

    positions = store.internal_positions(account, path)
    plan = plan_orders(target_weights, prices, positions, capital, buy_cost_rate=commission,
                       last_targets=store.last_targets(account, path), policy=policy,
                       max_weight_gap_pctp=max_weight_gap_pctp)
    for w in plan.warnings:  # 주문은 막지 않는다 — 사건으로 남겨 화면·리포트에서 보이게
        store.append_event(decision_date, account, "weight_gap_warning", w, path=path)
    for n in cap_notes:
        store.append_event(decision_date, account, "concentration_cap", n, path=path)
    result.warnings = cap_notes + list(plan.warnings)
    limited: list[str] = []
    if daily_limits is not None and not plan.rejected:
        keys = {f"{strategy}|{o.ticker}|{decision_date}|{account}" for o in plan.orders}
        limited = apply_daily_limits(plan, daily_limits, _daily_state(account, decision_date, sleeve, keys, path))
        for m in limited:
            store.append_event(decision_date, account, "daily_limit", m, path=path)
        result.warnings += limited
        if plan.rejected:  # 주문 횟수 초과 — 계획 전체를 보내지 않는다(cap_rejected 연속 집계와 별개)
            result.message = plan.reason
            _snapshot(broker, decision_date, account, tier.name, sleeve, capital, reference_value, path, result)
            return _finish(result, "daily_limit", decision_date, account, path)
    reason = plan.reason if plan.rejected else check_plan_within_cap(plan.orders, positions, prices, capital)
    if reason:
        store.append_event(decision_date, account, "cap_rejected", reason, path=path)
        store.append_engine_run(decision_date, account, "cap_rejected", reason, path=path)
        streak = store.trailing_status_streak(account, "cap_rejected", path)
        if streak % cap_rejected_streak == 0:
            store.append_event(decision_date, account, "order_error",
                               f"한도 초과 계획 연속 {streak}회(기준 {cap_rejected_streak}회)", path=path)
            _auto_demote(ladder, decision_date, account, benchmark_close, path, result)
        result.status, result.message = "cap_rejected", reason
        _snapshot(broker, decision_date, account, tier.name, sleeve, capital, reference_value, path, result)
        return result
    result.planned = list(plan.orders)

    if fill == "next_open":
        # 일일 제한으로 매수를 보류했으면 이번 목표를 "체결 완료"로 남기지 않는다 — 다음 결정이 다시 계산하게.
        targets = {} if limited else {t: target_weights.get(t, 0.0) for t in plan.target_qty}
        for o in plan.orders:
            key = f"{strategy}|{o.ticker}|{decision_date}|{account}"
            if not store.insert_order({"order_key": key, "decision_date": decision_date, "account": account,
                                       "tier": tier.name, "ticker": o.ticker, "side": o.side, "qty": o.qty,
                                       "ref_price": o.ref_price}, path=path):
                result.orders_skipped_duplicate += 1
        if plan.orders:
            store.add_pending_decision(decision_date, account, targets, path)
            result.message = f"주문 {len(plan.orders)}건 — 다음 거래일 시가 체결 대기"
        elif targets:
            store.append_targets(decision_date, account, targets, path)  # 거래가 없으면 결정 즉시 체결 완료 목표
        _snapshot(broker, decision_date, account, tier.name, sleeve, capital, reference_value, path, result)
        return _finish(result, store.freeze_state(account, path) or "ok", decision_date, account, path)

    complete = not limited  # 계획한 주문이 전부 체결됐는가(일일 제한으로 매수를 보류했으면 처음부터 아님) — 그래야 이번 목표를 "체결 완료 목표"로 기록한다
    for o in plan.orders:
        ks = kill_switch.engaged(kill_switch_file, path)
        if ks:
            store.append_event(decision_date, account, "kill_switch_stop", f"남은 주문 중단: {ks}", path=path)
            result.message = ks
            complete = False
            break
        key = f"{strategy}|{o.ticker}|{decision_date}|{account}"
        if not store.insert_order({"order_key": key, "decision_date": decision_date, "account": account,
                                   "tier": tier.name, "ticker": o.ticker, "side": o.side, "qty": o.qty,
                                   "ref_price": o.ref_price}, path=path):
            result.orders_skipped_duplicate += 1
            complete = False  # 같은 날 앞선 실행이 끝내지 못한 주문 — 상태를 모르니 완료로 치지 않는다
            continue
        complete &= _execute(broker, o, key, decision_date, account, commission, tax, path)
        result.orders_sent += 1

    try:
        problem = _mismatch(broker.get_positions(), store.internal_positions(account, path))
    except BrokerError as e:
        store.append_event(decision_date, account, "api_error", "주문 후 대사: " + str(e), path=path)
        problem = None
    if problem:
        sleeve = ladder_ops.sleeve_value(account, prices, path)
        _snapshot(broker, decision_date, account, tier.name, sleeve, capital, reference_value, path, result)
        return _handle_mismatch(ladder, decision_date, account, problem, "주문 후", benchmark_close, path, result)
    if complete:
        store.append_targets(decision_date, account, {t: target_weights.get(t, 0.0) for t in plan.target_qty}, path)

    _auto_demote(ladder, decision_date, account, benchmark_close, path, result)  # 이번 실행의 주문 오류 반영
    tier = ladder.get(store.current_tier(path)["to_tier"])
    sleeve = ladder_ops.sleeve_value(account, prices, path)
    capital = operating_capital(tier, account, sleeve, ladder)
    result.tier, result.sleeve_value, result.operating_capital = tier.name, sleeve, capital
    _snapshot(broker, decision_date, account, tier.name, sleeve, capital, reference_value, path, result)
    return _finish(result, store.freeze_state(account, path) or "ok", decision_date, account, path)


def _execute(broker: Broker, o, key: str, date: str, account: str, commission: float, tax: float,
             path: Path | None) -> bool:
    """
    주문 하나를 보내고 원장에 기록한다. 전량 체결이면 True.
    상태 기록 순서(재시작 복구용): 전송 직전 'sending' → 접수 'submitted'(주문번호) → 결과가 확정된 뒤에만 끝난 상태.
    잔량 취소가 실패하면 'submitted'로 남겨 다음 실행의 복구가 브로커에서 다시 확인하게 한다.
    """
    store.update_order(key, status="sending", path=path)
    try:
        st = broker.place_order(o.ticker, o.side, o.qty, o.ref_price)
    except BrokerError as e:
        store.update_order(key, status="error", path=path)
        store.append_event(date, account, "order_error", str(e), order_key=key, path=path)
        return False
    store.update_order(key, broker_order_id=st.broker_order_id,
                       status="rejected" if st.status == "rejected" else "submitted", path=path)
    if st.status == "rejected":
        store.append_event(date, account, "order_rejected", st.message, order_key=key, path=path)
        return False
    polls = 0
    while st.status == "submitted" and polls < MAX_POLLS:
        polls += 1
        try:
            st = broker.get_order_status(st.broker_order_id)
        except BrokerError as e:
            store.append_event(date, account, "order_error", "상태 조회: " + str(e), order_key=key, path=path)
            break
    still_open = False
    if st.status in ("submitted", "partial"):
        try:
            st = broker.cancel_order(st.broker_order_id)  # 남은 수량은 당일 취소 — 다음 날 몰래 체결되지 않게
        except BrokerError as e:
            store.append_event(date, account, "order_error", "취소: " + str(e), order_key=key, path=path)
            still_open = True
    if st.filled_qty > 0:
        gross = st.filled_qty * (st.avg_price or o.ref_price)
        if o.side == "buy":
            store.append_ledger(date, account, "buy", -gross * (1 + commission), o.ticker, st.filled_qty,
                                st.avg_price, key, path=path)
        else:
            store.append_ledger(date, account, "sell", gross * (1 - commission - tax), o.ticker, -st.filled_qty,
                                st.avg_price, key, path=path)
    if 0 < st.filled_qty < o.qty:
        store.append_event(date, account, "partial_fill", f"{st.filled_qty}/{o.qty}", order_key=key, path=path)
    store.update_order(key, status="submitted" if still_open else st.status, filled_qty=st.filled_qty, path=path)
    return st.filled_qty >= o.qty


def _snapshot(broker: Broker, date: str, account: str, tier: str, sleeve: float, capital: float,
              reference_value: float | None, path: Path | None, result: RunResult) -> None:
    try:
        account_value = broker.get_balance().total_value
    except BrokerError as e:
        store.append_event(date, account, "api_error", "잔고 조회: " + str(e), path=path)
        account_value = None
    result.account_value = account_value
    store.upsert_snapshot({
        "date": date, "account": account, "tier": tier, "account_value": account_value, "sleeve_value": sleeve,
        "operating_capital": capital,
        "outside_cash": None if account_value is None else max(0.0, account_value - capital),
        "reference_value": reference_value,
    }, path=path)


def execute_pending(broker: Broker, ladder: Ladder, fill_date: str, open_prices: dict[str, float], *,
                    valid_decision_dates: set[str] | None = None, commission: float = DEFAULT_COMMISSION,
                    tax: float = DEFAULT_TAX, benchmark_close=None, path: Path | None = None,
                    kill_switch_file: Path | None = kill_switch.DEFAULT_FILE) -> RunResult:
    """
    fill=next_open의 체결 단계: 이 계좌에서 fill_date보다 앞서 결정돼 아직 대기 중인 주문을 시가로 보낸다.

    - valid_decision_dates에 없는 결정(예: 실행을 하루 빠뜨려 이틀 지난 결정)은 보내지 않고 'expired'로 남긴다 —
      오래된 결정을 오늘 시가로 집행하지 않는다.
    - 매수할 현금이 모자라면(갭 상승) 살 수 있는 수량까지만 산다('qty_reduced' 사건) — analysis/what_if.simulate_shares의
      next_open 규칙과 같다. 이것은 오류가 아니다.
    - 모든 주문이 끝나면 그 결정의 목표 비중을 target_history에 "체결 완료 목표"로 적는다.
    - 킬 스위치·정지 상태면 보내지 않는다(대기 주문은 그대로 두고, 다음 날 지나면 expired).
    engine_runs에는 기록하지 않는다 — 결정 단계의 연속 cap_rejected 집계를 끊지 않게.
    """
    from executor.planner import PlannedOrder

    account = broker.account_kind
    ladder_ops.ensure_initialized(ladder, fill_date, path)
    result = RunResult(status="ok", tier=store.current_tier(path)["to_tier"])
    if not store.pending_decisions(account, before=fill_date, path=path) and not store.in_flight_orders(account, path):
        result.message = "체결 대기 주문 없음"
        return result
    ks = kill_switch.engaged(kill_switch_file, path)
    if ks:
        result.status, result.message = "blocked_kill_switch", ks
        return result
    frozen = store.freeze_state(account, path)
    if frozen:
        result.status, result.message = frozen, "정지 상태 — 대기 주문을 보내지 않음"
        return result
    if _recover(broker, fill_date, account, commission, tax, path, result):
        result.status = "halt"
        return result
    decisions = store.pending_decisions(account, before=fill_date, path=path)
    if not decisions:
        result.message = result.message or "체결 대기 주문 없음"
        return result

    live = []
    for d in decisions:
        if valid_decision_dates is not None and d["decision_date"] not in valid_decision_dates:
            for o in store.planned_orders(d["decision_date"], account, path):
                store.update_order(o["order_key"], status="expired", path=path)
            store.set_decision_status(d["decision_date"], account, "expired", path)
            store.append_event(fill_date, account, "order_expired",
                               f"{d['decision_date']} 결정 — 체결일 {fill_date}에는 보내지 않음(오래된 결정)", path=path)
        else:
            live.append(d)
    if not live:
        result.message = "대기 주문이 모두 만료됨"
        return result

    try:
        broker_pos = broker.get_positions()
    except BrokerError as e:
        store.append_event(fill_date, account, "api_error", str(e), path=path)
        result.status, result.message = "api_error", str(e)
        return result
    problem = _mismatch(broker_pos, store.internal_positions(account, path))
    if problem:
        return _handle_mismatch(ladder, fill_date, account, problem, "시가 체결 전", benchmark_close, path, result)

    for d in live:
        complete = True
        for row in store.planned_orders(d["decision_date"], account, path):
            ks = kill_switch.engaged(kill_switch_file, path)
            if ks:
                store.append_event(fill_date, account, "kill_switch_stop", f"남은 주문 중단: {ks}", path=path)
                result.message, complete = ks, False
                break
            price = open_prices.get(row["ticker"])
            if price is None or price <= 0:
                store.append_event(fill_date, account, "order_error", f"{row['ticker']} 시가 없음",
                                   order_key=row["order_key"], path=path)
                complete = False
                continue
            qty = int(row["qty"])
            if row["side"] == "buy":
                affordable = int(math.floor(max(0.0, store.sleeve_cash(account, path)) / (price * (1 + commission)) + 1e-9))
                if affordable < qty:
                    store.append_event(fill_date, account, "qty_reduced",
                                       f"{row['ticker']} 매수 {qty}→{affordable}주(시가 {price:,.0f}에 현금 부족)",
                                       order_key=row["order_key"], path=path)
                    qty = affordable
                if qty <= 0:
                    store.update_order(row["order_key"], status="skipped_cash", path=path)
                    continue
            complete &= _execute(broker, PlannedOrder(row["ticker"], row["side"], qty, float(price)),
                                 row["order_key"], fill_date, account, commission, tax, path)
            result.orders_sent += 1
        if complete:
            store.append_targets(d["decision_date"], account, d["targets"], path)
        store.set_decision_status(d["decision_date"], account, "filled" if complete else "incomplete", path)

    try:
        problem = _mismatch(broker.get_positions(), store.internal_positions(account, path))
    except BrokerError as e:
        store.append_event(fill_date, account, "api_error", "시가 체결 후 대사: " + str(e), path=path)
        problem = None
    if problem:
        return _handle_mismatch(ladder, fill_date, account, problem, "시가 체결 후", benchmark_close, path, result)
    if _auto_demote(ladder, fill_date, account, benchmark_close, path, result):
        result.status = store.freeze_state(account, path) or "review_required"
    return result
