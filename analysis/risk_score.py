"""
위험 점수·오늘의 상태 — 켜진 신호의 가중치(analysis/weighting.py)를
risk/opportunity로 나눠 더하고 빼서 0~100 점수로 환산한다. **주문/매매는
하지 않는다**(원칙 2) — 상태 문구도 "지금 사라/팔아라"가 아니라 "오늘
신규 매수가 보류 상태인가"·"보유 중이라면 분할 매도를 검토할 단계인가"를
구분해서 보여주는 참고 상태다. 이 앱은 포트폴리오/보유 여부를 추적하지
않으므로 holding은 호출부가 명시적으로 넘겨야 한다(기본은 "보유 안 함"
가정) — 모르는 걸 안다고 하지 않는다.

**위험 점수와 기회 점수 분리(2026-10-18)**: 예전에는 위험 점수 =
(risk 합 − opportunity 합 + 100)/2 여서, 급락 직후 켜지는 opportunity 신호(3일 급락·
패닉일·박스권 하단)가 위험 점수를 깎아 상태가 "정상"으로 돌아갔다(what_if 백테스트
2026-10-14→10-15에서 확인). 이제 기본은 분리 방식이다:
  - 위험 점수 = (켜진 risk 가중치 합 + 100)/2 — 상태 판정은 이것만으로 한다.
    임계값 60/80과 그 의미(risk 가중치 합 20%p/60%p 이상)는 그대로다. 켜진 risk 신호가
    없으면 50(하한)이다.
  - 기회 점수 = (켜진 opportunity 가중치 합 + 100)/2 — 같은 눈금으로 나란히 보여주기만
    하고 상태 문구를 만들지 않는다.
예전 방식은 net_opportunity=True로 그대로 재현할 수 있다(전/후 비교용).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RISK_THRESHOLD = 60.0  # 보류 전환 기준점(시작 기본값, 백테스트로 데이터에 맞춰 고르지 않음)
DEFAULT_SELL_STAGE_2_THRESHOLD = 80.0  # 분할매도 1/3 -> 2/3 전환 기준점

# 같은 원천 지표에서 파생된 신호 그룹 — "쏠림 경고"용(5단계). 상관계수로
# 자동 탐지하지 않는다 — "같은 원천"이라는 건 데이터 상관이 아니라 설계
# 지식(어느 원본 시계열을 썼는지)이라 analysis/signals.py·scripts/
# seed_signals.py의 data_source 주석을 근거로 사람이 직접 지정한다. 지금은
# 사용자가 예로 든 US10Y(^TNX) 하나뿐이다 — analysis/signals.py의
# ksj_us10y_high() docstring에 "YJH_US10Y와 상관 높음"이라고 이미 적혀 있음.
UNDERLYING_INDICATOR_GROUPS: dict[str, list[str]] = {
    "US10Y(^TNX)": ["YJH_US10Y", "KSJ_US10Y_HIGH"],
}
DEFAULT_CONCENTRATION_RATIO = 0.6  # 그룹 가중치 합이 켜진 전체 가중치 합의 60% 넘으면 경고(시작 기본값)


@dataclass
class RiskState:
    risk_score: float  # 0~100 (분리 방식에서는 50~100, 50 = 켜진 risk 신호 없음)
    opportunity_score: float  # 0~100 (50 = 켜진 opportunity 신호 없음). 상태 판정에 안 씀
    risk_weight_sum: float  # 켜진 risk 신호 가중치 합(%)
    opportunity_weight_sum: float  # 켜진 opportunity 신호 가중치 합(%)
    combo_triggered: bool  # KHJ_COMBO==3
    new_buy_status: str  # '매수 가능' | '보류'
    sell_stage_if_holding: str | None  # None | '분할 매도 1/3 단계' | '분할 매도 2/3 단계'
    on_signals: list[dict]  # [{signal_id, direction, weight_pct}, ...] — 대시보드 "켜진 신호 목록"용
    contributions: list[dict]  # [{signal_id, direction, weight_pct, score_contribution}, ...] — 5단계: 점수 분해
    concentration_warnings: list[dict]  # [{group, signals, weight_pct, share_of_total_pct}, ...] — 5단계: 쏠림 경고
    net_opportunity: bool = False  # True면 예전 방식(opportunity가 위험 점수를 깎음)


def score_contributions(on_signals: dict[str, dict], net_opportunity: bool = False) -> list[dict]:
    """
    각 켜진 신호가 위험 점수에 얼마나 기여했는지 분해한다(5단계: "점수
    분해를 대시보드에 바 형태로"). compute_risk_score()의 공식
    (score = (risk합-opportunity합+100)/2, 중립 50점)을 그대로 풀면, 신호
    하나의 점수 기여도는 risk면 +weight_pct/2, opportunity면 -weight_pct/2
    다(50점에서 얼마나 움직였는지). 모든 기여도를 더하면 50 + 합계 =
    클램프(0~100) 전의 raw score와 같다 — 점수가 0 또는 100에서 잘렸을
    때만 실제 risk_score와 정확히 안 맞을 수 있다(클램프 때문).

    분리 방식(기본)에서는 opportunity 신호가 위험 점수가 아니라 기회 점수에 +weight_pct/2로
    기여한다 — 각 항목의 "score"가 어느 점수에 기여하는지('risk'|'opportunity')를 말한다.
    """
    contributions = []
    for sid, v in on_signals.items():
        half = v["weight_pct"] / 2
        if v["direction"] == "risk":
            score, contrib = "risk", half
        elif net_opportunity:
            score, contrib = "risk", -half
        else:
            score, contrib = "opportunity", half
        contributions.append({
            "signal_id": sid, "direction": v["direction"], "weight_pct": v["weight_pct"],
            "score": score, "score_contribution": contrib,
        })
    return sorted(contributions, key=lambda c: abs(c["score_contribution"]), reverse=True)


def concentration_warnings(
    on_signals: dict[str, dict],
    groups: dict[str, list[str]] | None = None,
    ratio_threshold: float = DEFAULT_CONCENTRATION_RATIO,
) -> list[dict]:
    """
    같은 원천 지표에서 파생된 신호들이 동시에 켜지면, 서로 다른 신호처럼
    보이지만 사실 같은 정보 하나를 두 번 반영하는 것일 수 있다는 경고
    (5단계 요청, 예: US10Y). groups는 UNDERLYING_INDICATOR_GROUPS 기본값 —
    상관계수로 자동 탐지하지 않고 사람이 지정한 그룹만 본다.

    경고 기준: 그룹에 속한 "켜진" 신호가 2개 이상이고(혼자 켜진 건 쏠림이
    아니다), 그 신호들의 가중치 합이 켜진 전체 신호 가중치 합의
    ratio_threshold(기본 60%)를 넘으면 경고. 켜진 신호가 하나도 없으면
    (분모 0) 경고 없음.
    """
    groups = groups if groups is not None else UNDERLYING_INDICATOR_GROUPS
    total_on_weight = sum(v["weight_pct"] for v in on_signals.values())
    warnings: list[dict] = []
    if total_on_weight <= 0:
        return warnings
    for group_name, members in groups.items():
        on_members = [sid for sid in members if sid in on_signals]
        if len(on_members) < 2:
            continue
        group_weight = sum(on_signals[sid]["weight_pct"] for sid in on_members)
        share = group_weight / total_on_weight
        if share > ratio_threshold:
            warnings.append({
                "group": group_name, "signals": on_members,
                "weight_pct": group_weight, "share_of_total_pct": share * 100,
            })
    return warnings


def _to_score(raw: float) -> float:
    return max(0.0, min(100.0, (raw + 100) / 2))


def compute_risk_score(
    on_signal_weights: dict[str, float], directions: dict[str, str], net_opportunity: bool = False,
) -> tuple[float, float, float]:
    """
    (0~100 위험 점수, 켜진 risk 가중치 합, 켜진 opportunity 가중치 합).
    raw = risk합(분리 방식, 기본) 또는 risk합 - opportunity합(net_opportunity=True, 예전 방식)을
    (raw+100)/2로 0~100에 선형 매핑한다.
    """
    risk_w = sum(w for sid, w in on_signal_weights.items() if directions.get(sid) == "risk")
    opp_w = sum(w for sid, w in on_signal_weights.items() if directions.get(sid) == "opportunity")
    raw = risk_w - opp_w if net_opportunity else risk_w
    return _to_score(raw), risk_w, opp_w


def compute_opportunity_score(opportunity_weight_sum: float) -> float:
    """기회 점수 — 위험 점수와 같은 눈금((합+100)/2, 50 = 켜진 신호 없음). 상태 판정에 안 쓴다."""
    return _to_score(opportunity_weight_sum)


def determine_status(
    risk_score: float,
    combo_triggered: bool,
    risk_threshold: float = DEFAULT_RISK_THRESHOLD,
    sell_stage_2_threshold: float = DEFAULT_SELL_STAGE_2_THRESHOLD,
) -> tuple[str, str | None]:
    """(신규 매수 상태, 보유 중이라면의 분할매도 단계 — 보유 여부를 몰라도
    둘 다 같이 보여줄 수 있게 분리해서 반환). combo_triggered(KHJ_COMBO==3)
    이면 기준과 무관하게 분할매도 단계로 간다(사용자 지정)."""
    if combo_triggered:
        return "보류", "분할 매도 단계 (KHJ_COMBO)"
    if risk_score < risk_threshold:
        return "매수 가능", None
    stage = "분할 매도 2/3 단계" if risk_score >= sell_stage_2_threshold else "분할 매도 1/3 단계"
    return "보류", stage


def build_risk_state(
    on_signals: dict[str, dict],  # signal_id -> {"direction":..., "weight_pct":...}
    combo_value: float | None = None,
    risk_threshold: float = DEFAULT_RISK_THRESHOLD,
    sell_stage_2_threshold: float = DEFAULT_SELL_STAGE_2_THRESHOLD,
    net_opportunity: bool = False,
) -> RiskState:
    """
    on_signals: 그 날 is_on=1인 신호들만(signal_id -> direction/weight_pct).
    combo_value: KHJ_COMBO 신호의 그 날 value(동시에 켜진 개수, 0~3). None이면
    트리거 없음으로 간주.
    """
    weights = {sid: v["weight_pct"] for sid, v in on_signals.items()}
    directions = {sid: v["direction"] for sid, v in on_signals.items()}
    score, risk_w, opp_w = compute_risk_score(weights, directions, net_opportunity)
    combo_triggered = bool(combo_value is not None and combo_value >= 3)
    new_buy_status, sell_stage = determine_status(score, combo_triggered, risk_threshold, sell_stage_2_threshold)

    return RiskState(
        risk_score=score, opportunity_score=compute_opportunity_score(opp_w), risk_weight_sum=risk_w, opportunity_weight_sum=opp_w,
        combo_triggered=combo_triggered, new_buy_status=new_buy_status,
        sell_stage_if_holding=sell_stage,
        on_signals=[{"signal_id": sid, "direction": directions[sid], "weight_pct": weights[sid]} for sid in on_signals],
        contributions=score_contributions(on_signals, net_opportunity),
        concentration_warnings=concentration_warnings(on_signals),
        net_opportunity=net_opportunity,
    )


# KHJ_COMBO(=3단계 트리거)가 세는 하위 신호 — analysis/signals.khj_combo()와 같은 목록.
COMBO_COMPONENTS = ("KHJ_RATE_STAGE", "KHJ_CONCENTRATION", "KHJ_IPO_WEAK")


def attainable_ceiling(
    weights: dict[str, float],
    directions: dict[str, str],
    available: set[str],
    risk_threshold: float = DEFAULT_RISK_THRESHOLD,
    sell_stage_2_threshold: float = DEFAULT_SELL_STAGE_2_THRESHOLD,
    combo_components: tuple[str, ...] = COMBO_COMPONENTS,
) -> dict:
    """
    지금 값이 있는 신호(available)가 **전부 켜져도** 위험 점수가 어디까지 오를 수 있는지와, 그
    때문에 판정할 수 없는 단계. 수동 입력 누락 등으로 값이 없는 risk 신호는 켜질 수 없으므로
    상한을 낮춘다(분리 방식 점수 — opportunity는 위험 점수에 안 들어감).

    반환: {max_score, max_score_if_complete, missing_risk(값 없는 risk 신호, 가중치 있는 것만),
           missing_combo(값 없는 KHJ_COMBO 하위 신호), unreachable([{stage, missing, structural}])}
    structural=True면 누락을 다 채워도 현재 가중치로는 도달 못 한다는 뜻(누락 탓이 아님).
    """
    risk_ids = [sid for sid, d in directions.items() if d == "risk"]
    total = sum(weights.get(sid, 0.0) for sid in risk_ids)
    present = sum(weights.get(sid, 0.0) for sid in risk_ids if sid in available)
    missing_risk = sorted(sid for sid in risk_ids if sid not in available and weights.get(sid, 0.0) > 0)
    missing_combo = [sid for sid in combo_components if sid not in available]
    max_score, complete = _to_score(present), _to_score(total)
    unreachable = []
    for label, threshold in (("1단계", risk_threshold), ("2단계", sell_stage_2_threshold)):
        if max_score < threshold:
            unreachable.append({"stage": label, "missing": missing_risk, "structural": complete < threshold})
    if missing_combo:
        unreachable.append({"stage": "3단계", "missing": missing_combo, "structural": False})
    return {
        "max_score": max_score, "max_score_if_complete": complete,
        "missing_risk": missing_risk, "missing_combo": missing_combo, "unreachable": unreachable,
    }
