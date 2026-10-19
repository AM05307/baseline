"""
승격 기준(config/promotion_criteria.yaml) 평가 — 순수 함수.

"승격 가능"은 세 가지가 모두 참일 때만: (1) 해당 stage 기준이 confirmed, (2) 다음 단계가 확정
(confirmed + 금액 확정), (3) 모든 항목 PASS. 하나라도 아니면 blockers에 이유를 적는다.
값이 없는 지표(예: 비교 기준이 없어 괴리를 못 구함)는 PASS가 아니라 FAIL(데이터 없음)이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from executor.tiers import UNCONFIRMED, Ladder, TierMetrics, _num

# (키, 화면 이름, 비교 방식) — ge: 실제 ≥ 기준, le: 실제 ≤ 기준
CRITERIA_ITEMS = [
    ("min_days", "운용 일수", "ge"),
    ("min_trades", "거래 수", "ge"),
    ("max_mdd_ratio_vs_buy_hold", "MDD / 단순 보유 MDD", "le"),
    ("max_divergence_pct", "비교 기준 대비 괴리(%p)", "le"),
    ("max_order_errors", "주문 오류(건)", "le"),
    ("max_reconciliation_mismatches", "잔고 불일치(건)", "le"),
]


@dataclass
class CriteriaItem:
    key: str
    label: str
    threshold: float | None
    actual: float | None
    passed: bool
    note: str = ""


@dataclass
class PromotionStatus:
    tier: str
    next_tier: str | None
    stage: str | None
    criteria_confirmed: bool
    items: list[CriteriaItem] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def all_pass(self) -> bool:
        return bool(self.items) and all(i.passed for i in self.items)

    @property
    def eligible(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "tier": self.tier, "next_tier": self.next_tier, "stage": self.stage,
            "criteria_confirmed": self.criteria_confirmed, "eligible": self.eligible, "blockers": self.blockers,
            "items": [vars(i) for i in self.items],
        }


def _actual(key: str, m: TierMetrics) -> tuple[float | None, str]:
    if key == "min_days":
        return float(m.days), ""
    if key == "min_trades":
        return float(m.trades), ""
    if key == "max_mdd_ratio_vs_buy_hold":
        if m.mdd_pct is None or m.bh_mdd_pct is None:
            return None, "데이터 없음(MDD 또는 단순 보유 MDD)"
        if m.bh_mdd_pct == 0:
            return (0.0, "") if m.mdd_pct == 0 else (float("inf"), "단순 보유 MDD가 0")
        return abs(m.mdd_pct) / abs(m.bh_mdd_pct), ""
    if key == "max_divergence_pct":
        return (m.divergence_pct, "") if m.divergence_pct is not None else (None, "데이터 없음(비교 기준 없음)")
    if key == "max_order_errors":
        return float(m.order_errors), ""
    if key == "max_reconciliation_mismatches":
        return float(m.reconciliation_mismatches), ""
    raise KeyError(key)


def _mdd_ratio(m: TierMetrics, bench: dict) -> tuple[float | None, str]:
    if m.mdd_pct is None or bench.get("mdd_pct") is None:
        return None, "데이터 없음(MDD 또는 단순 보유 MDD)"
    if bench["mdd_pct"] == 0:
        return (0.0, "") if m.mdd_pct == 0 else (float("inf"), "단순 보유 MDD가 0")
    return abs(m.mdd_pct) / abs(bench["mdd_pct"]), ""


def evaluate_items(stage_cfg: dict, m: TierMetrics) -> list[CriteriaItem]:
    """MDD 비교 항목은 비교 종목마다 하나씩 만든다 — 하나라도 FAIL(데이터 없음 포함)이면 승격 불가."""
    items = []
    for key, label, op in CRITERIA_ITEMS:
        raw = stage_cfg.get(key)
        if key not in stage_cfg:
            continue
        threshold = _num(raw)
        if key == "max_mdd_ratio_vs_buy_hold" and m.benchmarks:
            for name, bench in m.benchmarks.items():
                actual, note = _mdd_ratio(m, bench)
                lab = f"{label} ({name})"
                if threshold is None:
                    items.append(CriteriaItem(key, lab, None, actual, False, UNCONFIRMED))
                elif actual is None:
                    items.append(CriteriaItem(key, lab, threshold, None, False, note))
                else:
                    items.append(CriteriaItem(key, lab, threshold, actual, actual <= threshold, note))
            continue
        actual, note = _actual(key, m)
        if threshold is None:
            items.append(CriteriaItem(key, label, None, actual, False, UNCONFIRMED))
            continue
        if actual is None:
            items.append(CriteriaItem(key, label, threshold, None, False, note))
            continue
        passed = actual >= threshold if op == "ge" else actual <= threshold
        items.append(CriteriaItem(key, label, threshold, actual, passed, note))
    return items


def promotion_status(ladder: Ladder, criteria_cfg: dict, tier_name: str, m: TierMetrics) -> PromotionStatus:
    tier = ladder.get(tier_name)
    nxt = ladder.next(tier_name)
    stage = tier.promotion_criteria
    stages = (criteria_cfg or {}).get("stages", {})
    stage_cfg = stages.get(stage) if stage else None
    st = PromotionStatus(tier=tier_name, next_tier=nxt.name if nxt else None, stage=stage,
                         criteria_confirmed=bool(stage_cfg and stage_cfg.get("confirmed")))
    if nxt is None:
        st.blockers.append("다음 단계 없음(마지막 단계)")
        return st
    if not nxt.promotable_to:
        st.blockers.append(f"다음 단계 {nxt.name} 미확정(금액 또는 confirmed)")
    if stage_cfg is None:
        st.blockers.append(f"승격 기준 stage '{stage}'가 promotion_criteria.yaml에 없음")
        return st
    if not st.criteria_confirmed:
        st.blockers.append(f"승격 기준 '{stage}' 미확정(confirmed: false)")
    st.items = evaluate_items(stage_cfg, m)
    failed = [i.label for i in st.items if not i.passed]
    if failed:
        st.blockers.append("FAIL 항목: " + ", ".join(failed))
    if not st.items:
        st.blockers.append("평가할 기준 항목이 없음")
    return st
