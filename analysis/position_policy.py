"""
포지션 정책 — 목표 비중을 보유 수량으로 바꾸는 규칙(순수 함수). **백테스트(analysis/what_if)와 실행
(executor/planner)이 둘 다 이 모듈을 쓴다**(docs/reference_gap_analysis.md 7절: 두 곳이 각자 규칙을 갖고 있어서
섀도 괴리에 구현 차이가 섞이던 문제). 주문을 내지 않는다 — 수량을 계산할 뿐이다(원칙 2).

규칙(순서대로):
  1. 목표 비중 0 → 수량 0.
  2. rebalance="on_target_change"이고 직전에 체결까지 끝낸 목표 비중(last_target_weight)과 같으면 지금 수량을
     그대로 둔다 — 가격이 움직여 실제 비중이 목표에서 벗어나는 것(드리프트)은 맞추지 않는다(what_if의 기존 규칙).
     단, 지금 보유 평가액이 운용 자본을 넘으면(손실·강등으로 자본이 줄었을 때) 다시 계산한다 — 한도 검사
     (planner.check_plan_within_cap)가 보유만으로 거부하는 일이 없게.
     rebalance="daily"면 매일 다시 계산한다.
  3. 이상적 수량 = 목표 비중 × 자본 / (가격 × (1 + 매수 비용률)) — 비중 100%여도 수수료까지 자본 안에 들어오게.
     lot_size가 0이면 소수점(비율) 그대로, 아니면 lot_size 단위로 내림.
  4. buffer > 0이면 |지금 수량 − 새 수량| ≤ buffer × 새 수량일 때 지금 수량을 둔다(pysystemtrade의 포지션 버퍼와
     같은 생각, 버퍼 가장자리가 아니라 목표까지 거래하는 단순형). 기본 0(끔) — 켤지와 크기는 사용자 결정.

기본값(on_target_change, 정수 주식, 버퍼 0)은 사용자 지정이 아니라 기존 what_if 결과를 바꾸지 않으려고 고른 값이다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EPS = 1e-9
REBALANCE_MODES = ("on_target_change", "daily")


@dataclass(frozen=True)
class PositionPolicy:
    rebalance: str = "on_target_change"
    lot_size: int = 1  # 0 = 소수점 수량(비율 백테스트용)
    buffer: float = 0.0
    buy_cost_rate: float = 0.0

    def __post_init__(self):
        if self.rebalance not in REBALANCE_MODES:
            raise ValueError(f"rebalance는 {REBALANCE_MODES} 중 하나: {self.rebalance}")
        if self.lot_size < 0 or self.buffer < 0 or self.buy_cost_rate < 0:
            raise ValueError(f"음수 설정: {self}")


def weight_changed(target_weight: float, last_target_weight: float | None) -> bool:
    """직전 목표가 없으면(처음·직전 실행 미완료) 바뀐 것으로 본다."""
    return last_target_weight is None or not math.isclose(target_weight, last_target_weight, abs_tol=1e-9)


def ideal_quantity(target_weight: float, price: float, capital: float, policy: PositionPolicy) -> float:
    if not 0.0 <= target_weight <= 1.0 + EPS:
        raise ValueError(f"목표 비중은 0~1: {target_weight}")
    if price <= 0:
        raise ValueError(f"가격은 양수: {price}")
    if capital <= 0 or target_weight <= 0:
        return 0.0
    q = target_weight * capital / (price * (1 + policy.buy_cost_rate))
    if policy.lot_size == 0:
        return q
    return float(math.floor(q / policy.lot_size + 1e-6) * policy.lot_size)


def target_quantity(current_qty: float, target_weight: float, last_target_weight: float | None, price: float,
                    capital: float, policy: PositionPolicy) -> float:
    """이 정책에서 오늘 가져야 할 수량. 반환값이 current_qty와 같으면 거래하지 않는다."""
    if target_weight <= 0:
        ideal_quantity(target_weight, price, capital, policy)  # 범위 검사만
        return 0.0
    over_capital = current_qty * price > capital + 1e-6
    if (policy.rebalance == "on_target_change" and not weight_changed(target_weight, last_target_weight)
            and not over_capital):
        return float(current_qty)
    q = ideal_quantity(target_weight, price, capital, policy)
    if policy.buffer > 0 and q > 0 and not over_capital and abs(current_qty - q) <= policy.buffer * q:
        return float(current_qty)
    return q


def realized_weight(target_weight: float, price: float, capital: float, policy: PositionPolicy) -> tuple[float, float]:
    """
    (이 정책으로 사게 되는 수량, 그 수량의 실현 비중). 실현 비중 = 수량 × 가격 / 운용 자본.
    주문 단위(정수 주식이면 1주) 때문에 목표 비중이 얼마나 깎이는지 보는 용도 — 드리프트와 무관하게 "지금 새로 맞춘다면"의 값.
    capital이 0 이하면 (0, 0).
    """
    if capital <= 0 or price <= 0:
        return 0.0, 0.0
    q = ideal_quantity(target_weight, price, capital, policy) if target_weight > 0 else 0.0
    return q, q * price / capital
