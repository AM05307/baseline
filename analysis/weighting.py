"""
신호 가중치 산정 — 백테스트 성적(analysis/signal_backtest.py)만큼만 가중치를
준다. 신호 이름이나 "전문가스러움"이 가중치를 정하지 않는다.

**신버전(2026-10-06 3차 세션, 현재 기본으로 쓰는 것)**: `compute_weights_significant()`.
적중률을 50%가 아니라 "아무 날이나 찍었을 때의 기준률"과 비교하고(edge),
부트스트랩 95% 신뢰구간 하한이 기준률을 넘지 못하거나 이벤트 수가
min_events(기본 30) 미만이면 가중치 0 — `raw_score_significant()` 참고.
analysis/signal_backtest.py의 통계 검증 결과(SignalPerformance)를 그대로
받는다.

**구버전(하위호환용으로 남김)**: `compute_weights()` + `raw_score()`.
    raw = max(0, 적중률 - 0.5) × n/(n+30)
50%를 고정 기준선으로 쓰고 통계적 유의성 검증이 없다 — 새 코드는 신버전을
쓸 것. 기존 테스트가 이 구버전 동작을 그대로 검증하고 있어서 지우지 않고
남겨뒀다.

상한(사용자 지정, 두 버전 공통): 신호 하나 최대 30%(신호별 override 가능,
예: KSJ_FOREIGN_SELL 15%), 전문가 한 명 합계 최대 40%. 상한을 넘는 만큼은
한도 안쪽인 나머지들에게 비중대로 재배분하고, 그래도 넘치면 반복한다
(아래 _clip_and_redistribute·_apply_expert_cap, 공통 로직은 `_distribute_and_cap`).
초기값(백테스트 history가 없어 raw가 전부 0일 때)은 모든 신호 동일 가중치 —
공식이 자연스럽게 그렇게 된다(사용자 지정 "초기값: 모든 신호 동일"과 일치).
"""

from __future__ import annotations

DEFAULT_SIGNAL_CAP_PCT = 30.0
DEFAULT_EXPERT_CAP_PCT = 40.0
MIN_SAMPLES_FOR_WEIGHT = 30  # 구버전 공식의 n/(n+30) 분모와 같은 수 — 원칙 3과 같은 기준
DEFAULT_MIN_EVENTS_FOR_WEIGHT = 30  # 신버전 공식(이벤트 단위, 사용자 지정 기본값)


def raw_score(hit_rate_pct: float, n: int) -> float:
    """max(0, 적중률-0.5) * n/(n+30). n=0이거나 적중률이 NaN이면 0."""
    if n <= 0 or hit_rate_pct != hit_rate_pct:  # NaN 체크
        return 0.0
    frac = hit_rate_pct / 100
    return max(0.0, frac - 0.5) * (n / (n + MIN_SAMPLES_FOR_WEIGHT))


def _waterfill_redistribute(
    weights: dict[str, float], excess: float, eligible: set[str], limits: dict[str, float],
) -> float:
    """
    eligible 키들에게 excess를 현재 비중대로 나눠주되, 각자 limits[k]를 넘지
    않게 한다 — 한도에 닿은 키는 그 라운드에서 빼고 남은 걸 나머지 eligible
    에게 다시 나눈다(water-filling, 한도가 겹겹이 있어도 안전하게 수렴).
    weights를 바로 수정하고, 끝내 못 나눠준 만큼(전원이 한도에 닿았을 때,
    즉 전체 합이 100%보다 작아지는 몫)을 반환한다.
    """
    active = set(eligible)
    while excess > 1e-9 and active:
        total_active = sum(weights[k] for k in active)
        if total_active <= 0:
            break
        newly_full = set()
        distributed = 0.0
        for k in list(active):
            share = excess * (weights[k] / total_active)
            headroom = max(0.0, limits[k] - weights[k])
            take = min(share, headroom)
            if take > 0:
                weights[k] += take
                distributed += take
            if take < share - 1e-9:
                newly_full.add(k)
        excess -= distributed
        if not newly_full:
            break
        active -= newly_full
    return excess


def _clip_and_redistribute(weights: dict[str, float], limits: dict[str, float]) -> tuple[dict[str, float], bool]:
    """한도 초과분을 한도로 고정하고, 초과분을 한도 안쪽인 나머지에게
    water-filling으로 재배분한다(각 대상도 자기 한도를 넘지 않게)."""
    weights = dict(weights)
    over = {k for k, v in weights.items() if v > limits[k] + 1e-9}
    if not over:
        return weights, False
    excess = sum(weights[k] - limits[k] for k in over)
    for k in over:
        weights[k] = limits[k]
    eligible = set(weights) - over
    _waterfill_redistribute(weights, excess, eligible, limits)
    return weights, True


def _apply_signal_caps(weights: dict[str, float], caps: dict[str, float], max_iter: int = 50) -> dict[str, float]:
    for _ in range(max_iter):
        weights, changed = _clip_and_redistribute(weights, caps)
        if not changed:
            break
    return weights


def _apply_expert_cap(
    weights: dict[str, float], expert_of: dict[str, str], expert_cap_pct: float,
) -> tuple[dict[str, float], bool]:
    """
    전문가 합계가 cap을 넘는 전문가들을 cap까지 비례 축소하고, 남는 몫을
    cap 밑인 전문가들에게 **전문가 단위로 water-filling**한 다음, 그 증가분을
    각 전문가 안에서 소속 신호들에게 비중대로 나눠 내려보낸다(2단계) — 이
    렇게 해야 재배분 때문에 다른 전문가가 다시 cap을 넘는 일이 없다.
    """
    weights = dict(weights)
    expert_sums: dict[str, float] = {}
    for k, v in weights.items():
        expert_sums[expert_of[k]] = expert_sums.get(expert_of[k], 0.0) + v
    over_experts = {e for e, s in expert_sums.items() if s > expert_cap_pct + 1e-9}
    if not over_experts:
        return weights, False

    excess = 0.0
    for k, v in list(weights.items()):
        e = expert_of[k]
        if e in over_experts:
            scale = expert_cap_pct / expert_sums[e]
            new_v = v * scale
            excess += v - new_v
            weights[k] = new_v

    under_experts = [e for e in expert_sums if e not in over_experts]
    if not under_experts:
        return weights, True  # 재배분할 전문가가 없음 — 합이 100% 밑으로 줄어듦(의도된 동작)

    expert_weights = {e: expert_sums[e] for e in under_experts}
    expert_limits = {e: expert_cap_pct for e in under_experts}
    _waterfill_redistribute(expert_weights, excess, set(under_experts), expert_limits)

    for e in under_experts:
        delta = expert_weights[e] - expert_sums[e]
        if delta <= 1e-12:
            continue
        members = [k for k in weights if expert_of[k] == e]
        member_total = sum(weights[k] for k in members)
        if member_total <= 0:
            continue
        for k in members:
            weights[k] += delta * (weights[k] / member_total)
    return weights, True


def _distribute_and_cap(
    raw: dict[str, float],
    expert_of: dict[str, str],
    signal_caps_pct: dict[str, float] | None,
    default_cap_pct: float,
    expert_cap_pct: float,
    max_outer_iter: int,
) -> dict[str, float]:
    """
    raw 점수(신호별, 음수 없음)를 100%에 비례 배분한 뒤 신호 cap·전문가 cap을
    반복 적용한다. compute_weights()·compute_weights_significant() 공통 로직
    (가중치를 만드는 "원점수"만 다르고 그 뒤 캡핑·정규화는 똑같다).
    """
    signal_caps_pct = signal_caps_pct or {}
    signal_ids = list(raw.keys())
    caps = {sid: signal_caps_pct.get(sid, default_cap_pct) for sid in signal_ids}

    total_raw = sum(raw.values())
    if total_raw <= 0:
        # 백테스트 history가 전혀 없거나 전부 기준선 이하 -> 동일 가중치(초기값)
        weights = {sid: 100.0 / len(signal_ids) for sid in signal_ids} if signal_ids else {}
    else:
        weights = {sid: raw[sid] / total_raw * 100 for sid in signal_ids}

    for _ in range(max_outer_iter):
        weights = _apply_signal_caps(weights, caps)
        weights, expert_changed = _apply_expert_cap(weights, expert_of, expert_cap_pct)
        # expert cap 재배분이 신호 cap을 다시 넘길 수 있어 다음 바퀴에서 또 체크한다.
        still_over_signal_cap = any(weights[sid] > caps[sid] + 1e-6 for sid in signal_ids)
        if not expert_changed and not still_over_signal_cap:
            break

    return weights


def compute_weights(
    hit_rates_pct: dict[str, float],
    sample_sizes: dict[str, int],
    expert_of: dict[str, str],
    signal_caps_pct: dict[str, float] | None = None,
    default_cap_pct: float = DEFAULT_SIGNAL_CAP_PCT,
    expert_cap_pct: float = DEFAULT_EXPERT_CAP_PCT,
    max_outer_iter: int = 50,
) -> dict[str, float]:
    """
    **구버전 공식**(기준선 고정 50%) — 하위호환을 위해 남겨둔다. 새로 가중치를
    산정할 때는 compute_weights_significant()를 쓸 것(2026-10-06 3차 세션:
    기준률·통계적 유의성 기반으로 교체, 아래 참고). signal_id -> 가중치(%,
    합계 ~100). signal_caps_pct에 override가 없는 신호는 default_cap_pct
    (기본 30%)를 쓴다.
    """
    signal_ids = list(hit_rates_pct.keys())
    raw = {sid: raw_score(hit_rates_pct[sid], sample_sizes.get(sid, 0)) for sid in signal_ids}
    return _distribute_and_cap(raw, expert_of, signal_caps_pct, default_cap_pct, expert_cap_pct, max_outer_iter)


def raw_score_significant(
    edge_pct: float, ci_lower_pct: float, base_rate_pct: float, n_events: int,
    min_events: int = DEFAULT_MIN_EVENTS_FOR_WEIGHT,
) -> float:
    """
    **신버전 공식**(2026-10-06 3차 세션, 사용자 지정): 신뢰구간 하한이
    기준률 이하이거나 이벤트 수가 min_events(기본 30) 미만이면 가중치 0.
    통과하면 max(0, edge) * n_events/(n_events+min_events) — 구버전의
    "50% 기준 적중률"을 "기준률 대비 edge"로, "0.5 이하면 0"을 "신뢰구간
    하한이 기준률을 못 넘으면 0"으로 바꾼 것 외엔 같은 형태(표본이 적으면
    0 쪽으로 축소)다.
    """
    if n_events < min_events:
        return 0.0
    if ci_lower_pct != ci_lower_pct or base_rate_pct != base_rate_pct:  # NaN -> 통계 불가
        return 0.0
    if ci_lower_pct <= base_rate_pct:
        return 0.0
    if edge_pct != edge_pct:
        return 0.0
    return max(0.0, edge_pct / 100) * (n_events / (n_events + min_events))


def compute_weights_significant(
    perf_by_signal: dict,  # signal_id -> analysis.signal_backtest.SignalPerformance
    expert_of: dict[str, str],
    signal_caps_pct: dict[str, float] | None = None,
    default_cap_pct: float = DEFAULT_SIGNAL_CAP_PCT,
    expert_cap_pct: float = DEFAULT_EXPERT_CAP_PCT,
    min_events: int = DEFAULT_MIN_EVENTS_FOR_WEIGHT,
    max_outer_iter: int = 50,
) -> dict[str, float]:
    """
    analysis/signal_backtest.py가 계산한 SignalPerformance(edge_pct·
    ci_lower_pct·base_rate_pct·n_events)로 가중치를 산정한다(신버전,
    raw_score_significant 참고). 캡핑·정규화는 구버전과 동일한 로직을
    공유한다(_distribute_and_cap).

    perf_by_signal에는 **한 horizon·한 window(보통 20거래일·in_sample 또는
    full)**의 SignalPerformance만 넣어야 한다 — 호출부가 어느 구간 성적으로
    가중치를 정할지 결정한다(walk-forward 검증 시 in_sample 성적만 넣고,
    그 가중치로 out_of_sample 성적을 비교하는 방식 — scripts/recompute_weights.py 참고).
    """
    signal_ids = list(perf_by_signal.keys())
    raw = {
        sid: raw_score_significant(
            perf_by_signal[sid].edge_pct, perf_by_signal[sid].ci_lower_pct,
            perf_by_signal[sid].base_rate_pct, perf_by_signal[sid].n_events, min_events,
        )
        for sid in signal_ids
    }
    return _distribute_and_cap(raw, expert_of, signal_caps_pct, default_cap_pct, expert_cap_pct, max_outer_iter)


def expert_weight_totals(weights: dict[str, float], expert_of: dict[str, str]) -> dict[str, float]:
    """signal_id별 가중치를 expert_id별로 합산. 대시보드 전문가별 표에 쓴다."""
    totals: dict[str, float] = {}
    for sid, w in weights.items():
        e = expert_of[sid]
        totals[e] = totals.get(e, 0.0) + w
    return totals
