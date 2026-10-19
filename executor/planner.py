"""
주문 계획 — 목표 비중 × 운용 자본 → 주문 수량(순수 함수).

목표 수량은 analysis/position_policy.target_quantity가 정한다 — 백테스트(analysis/what_if.simulate_shares)와 같은
함수다. 기본 정책은 "목표 비중이 바뀔 때만 다시 계산"(드리프트는 맞추지 않음)이고, 그러려면 직전에 체결까지 끝낸
목표 비중(last_targets)이 필요하다. 없으면(처음·직전 실행 미완료) 다시 계산한다.

운영 제한(2026-10-19, 기본값은 전부 미확정 — config/executor.yaml):
  - cap_single_weights(): 개별 종목 목표 비중 상한(기본 30%). 지수 ETF 예외 목록은 상한을 받지 않는다.
  - apply_daily_limits(): 일일 주문 횟수(넘으면 계획 전체 거부) · 일일 손실률과 강등·정지 뒤 쿨다운(매수만 막고 매도는 허용).

한도 강제는 두 겹이다:
  1. plan_orders(): 목표 보유 평가액 합계가 운용 자본을 넘지 않게 수량을 내림으로 계산한다.
  2. check_plan_within_cap(): 어디서 만든 계획이든 보내기 직전에 "주문 후 보유 평가액 합계 ≤ 운용 자본"과
     "매수 총액 ≤ 운용 자본"을 다시 확인한다. 넘으면 계획 전체를 거부한다(일부만 보내지 않음).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from analysis.position_policy import PositionPolicy, realized_weight, target_quantity

EPS = 1e-6
DEFAULT_MAX_WEIGHT_GAP_PCTP = 10.0  # 목표 비중과 실현 가능 비중 차이(%p) 경고 기준(config/executor.yaml max_weight_gap_pctp)


@dataclass(frozen=True)
class PlannedOrder:
    ticker: str
    side: str  # 'buy' | 'sell'
    qty: int
    ref_price: float

    @property
    def notional(self) -> float:
        return self.qty * self.ref_price


@dataclass
class Plan:
    capital: float
    orders: list[PlannedOrder] = field(default_factory=list)
    target_qty: dict[str, int] = field(default_factory=dict)
    rejected: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)  # 주문을 막지 않는 경고(주문 단위 때문에 목표 비중을 못 맞춤 등)


def weight_gap_warnings(target_weights: dict[str, float], prices: dict[str, float], capital: float,
                        policy: PositionPolicy, max_gap_pctp: float = DEFAULT_MAX_WEIGHT_GAP_PCTP) -> list[str]:
    """
    주문 단위(정수 주식) 때문에 목표 비중과 실제로 맞출 수 있는 비중의 차이가 max_gap_pctp(%p)를 넘는 종목.
    예: 000660 127만원·운용 자본 200만원이면 목표 66%가 1주(63.6%)로, 33%는 0주(0%)로 실현된다 → 33%p 차이 경고.
    주문을 막지 않는다 — 단계 비중이 의도대로 작동하지 않는다는 사실을 알리는 용도.
    """
    out = []
    for t, w in sorted(target_weights.items()):
        price = prices.get(t)
        if not price or w <= 0:
            continue
        q, real = realized_weight(w, price, capital, policy)
        gap = (w - real) * 100
        if abs(gap) > max_gap_pctp:
            out.append(f"{t}: 목표 {w:.0%} → {int(q)}주 = 실현 {real:.1%} (차이 {gap:+.1f}%p > {max_gap_pctp:g}%p, "
                       f"가격 {price:,.0f} · 운용 자본 {capital:,.0f})")
    return out


def plan_orders(target_weights: dict[str, float], prices: dict[str, float],
                positions: dict[str, int], capital: float, buy_cost_rate: float = 0.0,
                last_targets: dict[str, float] | None = None, policy: PositionPolicy | None = None,
                max_weight_gap_pctp: float = DEFAULT_MAX_WEIGHT_GAP_PCTP) -> Plan:
    """buy_cost_rate: 매수 수수료율 — policy를 안 주면 기본 정책(목표 변경 시에만, 정수 주식, 버퍼 0)에 이 값을 넣는다.
    목표 수량을 price×(1+수수료)로 나눠, 비중 100%여도 수수료까지 자본 안에 들어오게 한다.
    last_targets: 종목별로 직전에 체결까지 끝낸 목표 비중(없는 종목은 다시 계산)."""
    plan = Plan(capital=capital)
    policy = policy or PositionPolicy(buy_cost_rate=buy_cost_rate)
    last_targets = last_targets or {}
    if policy.lot_size < 1:
        raise ValueError("주문 계획은 정수 주식만(lot_size >= 1) — 소수점 수량은 백테스트 전용")
    if any(w < 0 or w > 1 for w in target_weights.values()) or sum(target_weights.values()) > 1 + EPS:
        plan.rejected, plan.reason = True, f"목표 비중이 0~1 범위를 벗어나거나 합이 1 초과: {target_weights}"
        return plan
    tickers = sorted(set(target_weights) | set(positions))
    for t in tickers:
        price = prices.get(t)
        if price is None or price <= 0:
            plan.rejected, plan.reason = True, f"{t} 가격 없음"
            return plan
        cur = positions.get(t, 0)
        q = target_quantity(cur, target_weights.get(t, 0.0), last_targets.get(t), price, capital, policy)
        plan.target_qty[t] = int(round(q))
        delta = plan.target_qty[t] - cur
        if delta:
            plan.orders.append(PlannedOrder(t, "buy" if delta > 0 else "sell", abs(delta), float(price)))
    plan.orders.sort(key=lambda o: o.side != "sell")  # 매도 먼저(현금 확보)
    plan.warnings = weight_gap_warnings(target_weights, prices, capital, policy, max_weight_gap_pctp)
    return plan


def check_plan_within_cap(orders: list[PlannedOrder], positions: dict[str, int],
                          prices: dict[str, float], capital: float) -> str | None:
    """한도를 넘으면 거부 사유 문자열, 괜찮으면 None."""
    after = dict(positions)
    buy_total = 0.0
    for o in orders:
        if o.qty <= 0 or o.side not in ("buy", "sell"):
            return f"잘못된 주문: {o}"
        after[o.ticker] = after.get(o.ticker, 0) + (o.qty if o.side == "buy" else -o.qty)
        if o.side == "buy":
            buy_total += o.notional
        if after[o.ticker] < 0:
            return f"{o.ticker} 보유보다 많이 매도"
    exposure = sum(q * prices[t] for t, q in after.items() if q)
    if exposure > capital + EPS:
        return f"주문 후 보유 평가액 {exposure:,.0f}원 > 운용 자본 {capital:,.0f}원"
    if buy_total > capital + EPS:
        return f"매수 총액 {buy_total:,.0f}원 > 운용 자본 {capital:,.0f}원"
    return None


# ---------------------------------------------------------------- 운영 제한 (기본값 미확정)

def cap_single_weights(target_weights: dict[str, float], max_single_weight: float | None,
                       exempt: frozenset[str] | set[str] = frozenset()) -> tuple[dict[str, float], list[str]]:
    """
    개별 종목의 목표 비중을 max_single_weight로 자른다(남는 비중은 현금). exempt(지수 ETF 등)는 자르지 않는다.
    max_single_weight가 None이면 자르지 않는다. 반환: (자른 비중, 알림 문구).
    """
    if max_single_weight is None:
        return dict(target_weights), []
    if not 0 < max_single_weight <= 1:
        raise ValueError(f"max_single_weight는 0 초과 1 이하: {max_single_weight}")
    out, notes = {}, []
    for t, w in target_weights.items():
        if t not in exempt and w > max_single_weight + EPS:
            out[t] = max_single_weight
            notes.append(f"{t}: 목표 {w:.0%} → 종목당 상한 {max_single_weight:.0%}(나머지는 현금)")
        else:
            out[t] = w
    return out, notes


@dataclass(frozen=True)
class DailyLimits:
    """None/0이면 그 제한은 끈다. 값은 config/executor.yaml daily_limits(시작 기본값, 미확정)."""
    max_daily_loss_pct: float | None = None  # 운용분이 직전 스냅샷 대비 이 % 이상 줄었으면 그날 매수 금지
    max_orders_per_day: int | None = None  # 같은 결정일 주문 수(이미 기록된 것 + 이번 계획)가 넘으면 계획 전체 거부
    cooldown_days: int = 0  # 마지막 강등·정지 사건일로부터 이 달력일 수 이내면 매수 금지

    @classmethod
    def from_config(cls, cfg: dict | None) -> "DailyLimits":
        cfg = cfg or {}
        loss, n = cfg.get("max_daily_loss_pct"), cfg.get("max_orders_per_day")
        return cls(None if loss in (None, 0) else float(loss), None if n in (None, 0) else int(n),
                   int(cfg.get("cooldown_days", 0) or 0))


@dataclass(frozen=True)
class DailyState:
    sleeve_change_pct: float | None = None  # 직전 스냅샷 대비 운용분 변화율(%) — 음수가 손실
    orders_today: int = 0  # 이번 계획과 겹치지 않는, 같은 결정일에 이미 기록된 주문 수
    days_since_freeze: int | None = None  # 마지막 강등·정지 사건 이후 달력일 수(없으면 None)


def apply_daily_limits(plan: Plan, limits: DailyLimits, state: DailyState) -> list[str]:
    """
    plan을 제자리에서 고친다. 반환: 적용된 제한 사유(빈 리스트면 아무것도 바꾸지 않음).
    매도는 위험을 줄이는 쪽이라 손실·쿨다운 제한에서도 남긴다. 주문 횟수 초과는 일부만 보내지 않도록 계획 전체를 거부한다.
    """
    reasons = []
    if limits.max_orders_per_day is not None and plan.orders \
            and state.orders_today + len(plan.orders) > limits.max_orders_per_day:
        plan.rejected = True
        plan.reason = (f"일일 주문 횟수 한도: 기존 {state.orders_today}건 + 계획 {len(plan.orders)}건 > "
                       f"{limits.max_orders_per_day}건")
        plan.orders = []
        return [plan.reason]
    block = []
    if limits.max_daily_loss_pct is not None and state.sleeve_change_pct is not None \
            and state.sleeve_change_pct <= -limits.max_daily_loss_pct:
        block.append(f"일일 손실 한도: 운용분 {state.sleeve_change_pct:+.2f}% ≤ −{limits.max_daily_loss_pct:g}%")
    if limits.cooldown_days > 0 and state.days_since_freeze is not None and state.days_since_freeze < limits.cooldown_days:
        block.append(f"쿨다운: 강등·정지 뒤 {state.days_since_freeze}일 < {limits.cooldown_days}일")
    if block and any(o.side == "buy" for o in plan.orders):
        dropped = [o for o in plan.orders if o.side == "buy"]
        plan.orders = [o for o in plan.orders if o.side != "buy"]
        reasons = [f"{r} — 매수 {len(dropped)}건 보류(매도는 진행)" for r in block]
    return reasons
