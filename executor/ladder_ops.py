"""
자본 사다리 상태 변경 — 초기화·승격(수동)·강등(자동). 모든 변경은 tier_history에 append-only로 남는다.

승격: promote()는 (1) 그 시점에 승격 기준을 **다시 계산**해서 eligible이고 (2) 확인 문구가 정확히 일치할 때만
반영한다. promotion_report.py의 과거 PASS 기록만으로는 승격하지 않는다.
강등: demote()는 한 단계만 내리고, 사유에 따라 그 계좌를 정지한다(2026-10-18 사용자 결정):
  - 잔고 불일치 → 'halt': 정리 주문을 포함한 모든 주문 정지
  - MDD·괴리(그리고 주문 오류) → 'review_required': 신규 주문 정지 + 보유 유지 + 수동 판단 필요
    정리할지는 사람이 결정한다(executor.scripts.resume으로 해제하면 다음 실행부터 줄어든 운용 자본에 맞춰 주문).
재승격은 다시 promote() 절차를 거친다(자동 재승격 경로 없음).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from executor import store
from executor.criteria import PromotionStatus, promotion_status
from executor.metrics import tier_metrics_from_db
from executor.tiers import DemotionReason, Ladder, TierMetrics


class PromotionRefused(Exception):
    pass


def ensure_initialized(ladder: Ladder, effective_on: str, path: Path | None = None) -> dict:
    """단계 기록이 없으면 맨 아래 단계로 시작하고, 그 계좌에 그 단계 한도만큼 가상 자금을 배정한다."""
    cur = store.current_tier(path)
    if cur:
        return cur
    bottom = ladder.bottom
    store.append_tier_event(effective_on, "init", None, bottom.name, "auto", "최초 시작", path=path)
    if bottom.max_capital_krw:
        store.append_ledger(effective_on, bottom.account, "allocate", bottom.max_capital_krw,
                            note=f"{bottom.name} 가상 자금 배정", path=path)
    return store.current_tier(path)


def sleeve_value(account: str, prices: dict[str, float], path: Path | None = None) -> float:
    pos = store.internal_positions(account, path)
    missing = [t for t in pos if t not in prices]
    if missing:
        raise ValueError(f"운용분 평가에 필요한 가격 없음: {missing}")
    return store.sleeve_cash(account, path) + sum(q * prices[t] for t, q in pos.items())


def current_metrics(ladder: Ladder, benchmark_close=None, path: Path | None = None) -> tuple[dict, TierMetrics]:
    cur = store.current_tier(path)
    tier = ladder.get(cur["to_tier"])
    return cur, tier_metrics_from_db(tier.account, cur["effective_on"], benchmark_close=benchmark_close, path=path)


def freeze_kind_for(reasons: list[DemotionReason]) -> str:
    return "halt" if any(r.category == "mismatch" for r in reasons) else "review_required"


def demote(ladder: Ladder, effective_on: str, reasons: list[DemotionReason], account: str,
           path: Path | None = None) -> str | None:
    """
    한 단계 내리고 그 계좌를 정지한다(freeze_kind_for). 맨 아래 단계면 단계는 그대로(None 반환)지만 정지는 한다.
    """
    text = "; ".join(str(r) for r in reasons)
    kind = freeze_kind_for(reasons)
    label = "모든 주문 정지(정리 주문 포함)" if kind == "halt" else "신규 주문 정지·보유 유지 — 수동 판단 필요"
    store.append_event(effective_on, account, kind, f"{label}: {text}", path=path)
    cur = store.current_tier(path)["to_tier"]
    prev = ladder.prev(cur)
    if prev is None:
        return None
    store.append_tier_event(effective_on, "demote", cur, prev.name, "auto", text, path=path)
    return prev.name


def confirm_phrase(from_tier: str, to_tier: str) -> str:
    return f"{from_tier}에서 {to_tier}로 승격합니다"


def promote(ladder: Ladder, criteria_cfg: dict, effective_on: str, confirm_text: str,
            prices: dict[str, float], account_value: float | None = None,
            benchmark_close=None, path: Path | None = None) -> PromotionStatus:
    cur, m = current_metrics(ladder, benchmark_close, path)
    status = promotion_status(ladder, criteria_cfg, cur["to_tier"], m)
    store.append_promotion_result(status.tier, status.next_tier, status.eligible,
                                  {**status.to_dict(), "via": "promote_tier"}, path=path)
    if not status.eligible:
        raise PromotionRefused("승격 불가: " + " / ".join(status.blockers))
    nxt = ladder.get(status.next_tier)
    expected = confirm_phrase(status.tier, nxt.name)
    if (confirm_text or "").strip() != expected:
        raise PromotionRefused(f"확인 문구가 일치하지 않습니다. 정확히 입력: {expected}")
    for acct in {ladder.get(status.tier).account, nxt.account}:
        state = store.freeze_state(acct, path)
        if state:
            raise PromotionRefused(f"{acct} 계좌가 정지 상태({state})입니다 — 원인 확인 후 resume 먼저")
    if nxt.kind == "real" and account_value is None:
        raise PromotionRefused("실제 자금 단계로 올리려면 자동매매 계좌 평가금액(--account-value)이 필요합니다")

    target = min(float(account_value), nxt.max_capital_krw) if nxt.kind == "real" else nxt.max_capital_krw
    top_up = max(0.0, target - sleeve_value(nxt.account, prices, path))
    store.append_tier_event(effective_on, "promote", status.tier, nxt.name, "manual",
                            f"승격 기준 '{status.stage}' 전 항목 PASS · 확인 문구 입력", path=path)
    if top_up > 0:
        store.append_ledger(effective_on, nxt.account, "allocate", top_up,
                            note=f"{nxt.name} 승격 배정(목표 운용분 {target:,.0f}원)", path=path)
    return status
