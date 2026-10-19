"""
자본 사다리 — 단계 정의·운용 자본 계산·강등 판정·승격 가능 여부(순수 함수, DB 비의존).

핵심 규칙(EXECUTOR.md "자본 사다리"):
  - 주문 계산에 쓰는 자본 = min(운용분(슬리브) 평가금액, 현재 단계 한도).
    "계좌 평가금액"이 아니라 슬리브를 쓰는 이유: 계좌에는 한도 밖 현금이 함께 있어서, 계좌 평가금액으로
    min을 잡으면 운용분이 손실을 봐도 한도 밖 현금이 그 손실을 자동으로 메워버린다. 슬리브는 executor
    원장(배정액 + 매매 손익)으로만 움직이므로 손실이 나면 그 금액 그대로 운용한다.
  - 승격은 수동(이 모듈은 "가능한가"만 판정), 강등은 자동(한 단계씩).
  - confirmed가 아닌 단계·기준으로는 승격하지 않는다. 강등 기준은 미확정이어도 적힌 값으로 작동한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

UNCONFIRMED = "미확정"
REAL_ACCOUNT = "real"  # 실제 자금 단계는 모두 이 계좌 하나를 쓴다. 가상 단계는 단계 이름이 곧 계좌 이름(shadow·paper)


def _num(v) -> float | None:
    """숫자면 float, "미확정"·None이면 None."""
    if v is None or v == UNCONFIRMED:
        return None
    if isinstance(v, bool):
        raise ValueError(f"숫자 자리에 bool: {v}")
    return float(v)


@dataclass(frozen=True)
class DemotionRule:
    max_mdd_pct: float | None
    max_divergence_pct: float | None
    max_order_errors: float | None
    max_reconciliation_mismatches: float | None
    confirmed: bool


@dataclass(frozen=True)
class Tier:
    name: str
    kind: str  # 'virtual' | 'real'
    max_capital_krw: float | None  # None = 미확정
    confirmed: bool
    promotion_criteria: str | None
    demotion: DemotionRule | None
    index: int

    @property
    def account(self) -> str:
        """원장·스냅샷·주문이 기록되는 계좌. 섀도와 모의투자가 같은 원장을 쓰지 않게 가상 단계는 단계별로 나눈다."""
        return REAL_ACCOUNT if self.kind == "real" else self.name

    @property
    def promotable_to(self) -> bool:
        """이 단계로 승격할 수 있도록 확정됐는가(금액까지)."""
        return self.confirmed and self.max_capital_krw is not None


@dataclass
class Ladder:
    tiers: list[Tier]

    def get(self, name: str) -> Tier:
        for t in self.tiers:
            if t.name == name:
                return t
        raise KeyError(f"정의되지 않은 단계: {name}")

    def next(self, name: str) -> Tier | None:
        i = self.get(name).index
        return self.tiers[i + 1] if i + 1 < len(self.tiers) else None

    def prev(self, name: str) -> Tier | None:
        i = self.get(name).index
        return self.tiers[i - 1] if i > 0 else None

    @property
    def bottom(self) -> Tier:
        return self.tiers[0]

    @property
    def accounts(self) -> list[str]:
        return list(dict.fromkeys(t.account for t in self.tiers))


def parse_ladder(data: dict) -> Ladder:
    raw = (data or {}).get("tiers")
    if not raw:
        raise ValueError("capital_tiers.yaml에 tiers가 없습니다")
    tiers, names = [], set()
    for i, t in enumerate(raw):
        name = t["name"]
        if name == REAL_ACCOUNT:
            raise ValueError(f"단계 이름 '{REAL_ACCOUNT}'는 실제 자금 계좌 이름이라 쓸 수 없습니다")
        if name in names:
            raise ValueError(f"단계 이름 중복: {name}")
        names.add(name)
        kind = t["kind"]
        if kind not in ("virtual", "real"):
            raise ValueError(f"{name}: kind는 virtual|real")
        d = t.get("demotion")
        rule = None if d is None else DemotionRule(
            max_mdd_pct=_num(d.get("max_mdd_pct")), max_divergence_pct=_num(d.get("max_divergence_pct")),
            max_order_errors=_num(d.get("max_order_errors")),
            max_reconciliation_mismatches=_num(d.get("max_reconciliation_mismatches")),
            confirmed=bool(d.get("confirmed", False)),
        )
        cap = _num(t.get("max_capital_krw"))
        if cap is not None and cap < 0:
            raise ValueError(f"{name}: 한도는 0 이상")
        tiers.append(Tier(name=name, kind=kind, max_capital_krw=cap, confirmed=bool(t.get("confirmed", False)),
                          promotion_criteria=t.get("promotion_criteria"), demotion=rule, index=i))
    if tiers[0].kind != "virtual":
        raise ValueError("첫 단계는 가상(virtual)이어야 합니다")
    seen_real = False
    for t in tiers:
        if t.kind == "real":
            seen_real = True
        elif seen_real:
            raise ValueError("실제 자금 단계 뒤에 가상 단계가 올 수 없습니다")
    return Ladder(tiers)


def operating_capital(tier: Tier, account: str, sleeve_value: float, ladder: Ladder) -> float:
    """
    현재 단계(tier)일 때 이 계좌(account)에서 주문 계산에 쓸 자본.
      - 실제 계좌: 현재 단계가 실제 자금 단계면 min(슬리브, 그 단계 한도), 아니면 0(실제 돈은 운용하지 않음)
      - 가상 계좌(shadow·paper): 그 계좌의 단계에 이미 도달했으면 min(슬리브, 그 계좌 단계의 한도) — 더 높은
        단계로 올라간 뒤에도 비교 기준으로 계속 돌린다. 아직 도달하지 않았으면 0.
      - 한도가 미확정(None)이면 0
    """
    if account == REAL_ACCOUNT:
        cap = tier.max_capital_krw if tier.kind == "real" else 0.0
    else:
        acct_tier = ladder.get(account)
        if acct_tier.kind != "virtual":
            raise ValueError(account)
        cap = acct_tier.max_capital_krw if acct_tier.index <= tier.index else 0.0
    if cap is None:
        return 0.0
    return max(0.0, min(float(sleeve_value), float(cap)))


@dataclass
class TierMetrics:
    """단계 진입 후 성과(표본수 포함). 값을 구할 수 없으면 None — 지어내지 않는다."""
    entry_date: str | None
    last_date: str | None
    days: int
    trades: int
    cum_return_pct: float | None
    mdd_pct: float | None  # 0 이하(%)
    divergence_pct: float | None  # |실제 누적 − 비교 기준 누적| (%p)
    order_errors: int
    reconciliation_mismatches: int
    bh_return_pct: float | None = None  # 첫 번째 비교 종목(호환용) — 전체는 benchmarks
    bh_mdd_pct: float | None = None
    benchmarks: dict = field(default_factory=dict)  # {종목: {"return_pct":…, "mdd_pct":…}} — 값 없으면 None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DemotionReason:
    category: str  # 'mdd' | 'divergence' | 'order_errors' | 'mismatch'
    text: str

    def __str__(self) -> str:
        return self.text


def check_demotion(rule: DemotionRule | None, m: TierMetrics) -> list[DemotionReason]:
    """넘은 강등 조건(비어 있으면 강등 없음). 값이 없는 지표는 그 조건을 판정하지 않는다."""
    if rule is None:
        return []
    reasons = []
    if rule.max_mdd_pct is not None and m.mdd_pct is not None and -m.mdd_pct > rule.max_mdd_pct:
        reasons.append(DemotionReason("mdd", f"MDD {m.mdd_pct:.2f}% (기준 −{rule.max_mdd_pct:g}% 초과)"))
    if rule.max_divergence_pct is not None and m.divergence_pct is not None and m.divergence_pct > rule.max_divergence_pct:
        reasons.append(DemotionReason("divergence", f"비교 기준 대비 괴리 {m.divergence_pct:.2f}%p (기준 {rule.max_divergence_pct:g}%p 초과)"))
    if rule.max_order_errors is not None and m.order_errors > rule.max_order_errors:
        reasons.append(DemotionReason("order_errors", f"주문 오류 {m.order_errors}건 (기준 {rule.max_order_errors:g}건 초과)"))
    if rule.max_reconciliation_mismatches is not None and m.reconciliation_mismatches > rule.max_reconciliation_mismatches:
        reasons.append(DemotionReason("mismatch", f"잔고 불일치 {m.reconciliation_mismatches}건 (기준 {rule.max_reconciliation_mismatches:g}건 초과)"))
    return reasons
