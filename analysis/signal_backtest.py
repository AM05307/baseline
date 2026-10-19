"""
신호별 성적 추적 — analysis/signals.py가 계산한 on/off 신호가 과거에
실제로 의미가 있었는지 측정한다.

**이 모듈만은 미래 데이터를 쓴다** — 그런데 그게 당연하다. "신호 생성"은
항상 그 날짜까지의 데이터만 써야 하지만(analysis/signals.py가 지키는
원칙), "신호 성과 측정(백테스트)"은 정의상 신호가 켜진 날 *이후*에 무슨
일이 있었는지를 봐야 한다 — analysis/backtest.py의 event_window_returns와
같은 성격이다. 실시간으로 "오늘의 신호"를 계산할 때는 이 함수를 쓰지
않는다(그건 analysis/signals.py만 쓴다).

적중 정의(사용자 지정): risk 신호는 이후 수익률 < 0이면 적중,
opportunity 신호는 이후 수익률 > 0이면 적중.

"최대낙폭"에 대한 해석: 여기서는 여러 on-일의 N거래일 뒤 수익률을 모은
분포이지, 하나로 이어지는 자본 곡선이 아니다(온-일들이 겹칠 수 있어서
연속 복리 곡선을 만들 수 없음). 그래서 이 모듈의 "mdd_pct"는 엄밀한
낙폭이 아니라 **그 분포에서 가장 나빴던 단일 사례(최악의 수익률)**다 —
연속 신호를 한 사건으로 묶은 분석과 함께 보면 원래 뜻("최악의 경우 대비")
에 가깝게 쓰인다.

--- 통계 검증(2026-10-06 2차 세션 추가) ---

기준률(base rate): 신호를 50%와 비교하는 건 의미가 없다 — 애초에 000660이
그냥 "아무 날이나 찍었을 때" 오를 확률이 50%가 아닐 수 있다(상승장이면
그 자체로 나이브하게 55% 넘을 수 있음). 그래서 신호의 적중률을 0.5가 아니라
**같은 구간 전체 날짜의 기준률**(risk는 "아무 날이나 찍었을 때 하락할
비율", opportunity는 "오를 비율")과 비교한다. 그 차이를 edge라고 부른다.

표본 단위: 이항검정·부트스트랩 신뢰구간은 모두 **이벤트 단위**(연속 on
구간을 하나로 묶은 것, cluster_events)로 계산한다 — 일별 표본으로 하면
겹치는 N거래일 forward-window들끼리 강한 자기상관이 생겨서 표본이 실제보다
훨씬 많아 보이는 착시(가짜 유의성)가 생긴다. 이벤트는 서로 겹치지 않으므로
독립 근사가 더 타당하다.

이항검정: 이벤트 적중 횟수가 "기준률대로 무작위라면 나올 법한 범위"를
넘는지 one-sided(알파가 있는지만 본다, 음의 edge는 가중치 0으로 이미
걸러지므로 손해 보는 방향은 검정할 필요가 없음) 이항검정으로 본다
(scipy.stats.binomtest, alternative="greater").

부트스트랩 신뢰구간: 이벤트의 적중/실패 리스트를 복원추출로 재표본해서
적중률의 95% 신뢰구간을 만든다(이벤트 묶음 단위 부트스트랩 — 사용자가
제시한 두 선택지 중 이쪽을 택함, 블록 부트스트랩보다 구현이 단순하고
이벤트가 이미 비중복이라 왜곡이 적다).

다중비교 보정: 16개 신호 × 3개 기간(5/20/63일)을 동시에 검정하므로, 개별
p-value를 그대로 믿으면 우연히 유의해 보이는 신호가 나올 확률이 커진다
(multiple comparisons). Benjamini-Hochberg 절차로 q-value를 같이 낸다
(apply_benjamini_hochberg — 여러 SignalPerformance를 한 번에 받아야 해서
evaluate_signal() 밖의 별도 함수로 분리).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

MIN_SAMPLES = 30  # 원칙 3. 다른 analysis 모듈과 같은 기준값, 독립 상수(기존 관행).
DEFAULT_HORIZONS = (5, 20, 63)
PRIMARY_HORIZON = 20
OUT_OF_SAMPLE_YEARS = 1
DEFAULT_MIN_EVENTS = 30  # 가중치 산정용 최소 이벤트 수(사용자 지정 기본값)
DEFAULT_N_BOOT = 2000
DEFAULT_BOOTSTRAP_SEED = 0


@dataclass
class SignalPerformance:
    direction: str  # 'risk' | 'opportunity'
    horizon: int
    n: int
    hit_rate_pct: float
    mean_return_pct: float
    median_return_pct: float
    worst_pct: float  # "최대낙폭"의 이 모듈 내 의미 — 모듈 docstring 참고
    reliable: bool  # 기존 의미 유지: 일별 표본 n >= MIN_SAMPLES
    n_events: int  # 연속 on 구간을 하나로 묶은 표본 수
    hit_rate_pct_events: float
    # --- 통계 검증 필드 ---
    base_rate_pct: float = float("nan")  # 같은 구간 전체 날짜 기준 "아무 날이나 찍었을 때"의 비율
    edge_pct: float = float("nan")  # hit_rate_pct_events - base_rate_pct
    p_value: float = float("nan")  # 이벤트 적중수가 기준률보다 높은지 one-sided 이항검정
    ci_lower_pct: float = float("nan")  # 이벤트 단위 부트스트랩 95% 신뢰구간 하한(적중률 %)
    ci_upper_pct: float = float("nan")
    q_value: float = float("nan")  # BH 보정, apply_benjamini_hochberg()가 나중에 채움(기본 NaN)
    significant: bool = False  # n_events>=min_events and ci_lower_pct>base_rate_pct


def forward_returns(close: pd.Series, horizon: int) -> pd.Series:
    """각 날짜의 horizon거래일 뒤까지 수익률(%). 마지막 horizon일은 NaN(미래 데이터 없음)."""
    close = close.sort_index()
    return (close.shift(-horizon) / close - 1) * 100


def cluster_events(is_on: pd.Series) -> pd.Series:
    """연속 on 구간의 '첫 날'만 True인 불리언 Series. 중복 표본(같은 사건을
    여러 날로 중복 카운트하는 것) 방지용 — "하나의 사건으로 묶은 버전"에 쓴다."""
    is_on = is_on.fillna(0).astype(int)
    prev = is_on.shift(1, fill_value=0)
    return (is_on == 1) & (prev == 0)


def _hits(returns: pd.Series, direction: str) -> pd.Series:
    return returns < 0 if direction == "risk" else returns > 0


def base_rate(window_returns: pd.Series, direction: str) -> float:
    """그 구간 전체 날짜(신호와 무관하게) 중 direction 조건이 맞을 비율(%).
    "아무 날이나 찍었을 때"의 기준선 — 신호의 적중률을 0.5가 아니라 이것과
    비교해야 진짜 edge를 알 수 있다."""
    valid = window_returns.dropna()
    if len(valid) == 0:
        return float("nan")
    return float(_hits(valid, direction).mean() * 100)


def binomial_p_value(n_events: int, hit_count: int, base_rate_frac: float) -> float:
    """
    이벤트 적중수가 기준률보다 '높다'(one-sided, alternative='greater')는
    이항검정 p-value. 음의 edge는 애초에 가중치를 안 주므로 그 방향은
    검정하지 않는다(의도적 선택, 모듈 docstring 참고).
    """
    if n_events <= 0 or base_rate_frac != base_rate_frac or not (0 < base_rate_frac < 1):
        return float("nan")
    return float(scipy_stats.binomtest(hit_count, n_events, base_rate_frac, alternative="greater").pvalue)


def bootstrap_ci(
    event_returns: pd.Series,
    direction: str,
    n_boot: int = DEFAULT_N_BOOT,
    ci: float = 0.95,
    random_state: int = DEFAULT_BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """
    이벤트(연속 on 구간을 하나로 묶은 것)의 적중/실패를 복원추출로
    재표본해서 적중률의 (1-ci) 양측 신뢰구간을 만든다. 이벤트 단위라서
    겹치는 forward-window의 자기상관 문제를 피한다(모듈 docstring 참고).
    이벤트가 0개면 (NaN, NaN).
    """
    n = len(event_returns)
    if n == 0:
        return float("nan"), float("nan")
    hits = _hits(event_returns, direction).to_numpy().astype(float)
    rs = np.random.RandomState(random_state)
    idx = rs.randint(0, n, size=(n_boot, n))
    boot_rates = hits[idx].mean(axis=1) * 100
    lo_pct, hi_pct = (1 - ci) / 2 * 100, (1 + ci) / 2 * 100
    return float(np.percentile(boot_rates, lo_pct)), float(np.percentile(boot_rates, hi_pct))


def benjamini_hochberg(p_values: dict[str, float]) -> dict[str, float]:
    """
    BH 절차로 q-value를 계산한다(다중비교 보정). p_values: {key: p_value}.
    NaN인 p_value는 보정 대상에서 빼고(표본 자체가 없다는 뜻) 결과에도
    NaN으로 돌려준다.
    """
    items = [(k, p) for k, p in p_values.items() if p == p]
    q: dict[str, float] = {k: float("nan") for k in p_values}
    m = len(items)
    if m == 0:
        return q
    items.sort(key=lambda kv: kv[1])
    raw_q = [p * m / (i + 1) for i, (_, p) in enumerate(items)]
    running_min = float("inf")
    adjusted = [0.0] * m
    for i in range(m - 1, -1, -1):
        running_min = min(running_min, raw_q[i])
        adjusted[i] = min(running_min, 1.0)
    for (k, _), adj in zip(items, adjusted):
        q[k] = adj
    return q


def apply_benjamini_hochberg(results: list[SignalPerformance]) -> None:
    """results의 p_value들을 모아 BH q-value를 계산하고 각 SignalPerformance.q_value에
    제자리로 채운다(리스트 안의 객체를 직접 수정 — 반환값 없음). 신호×기간을
    한 '검정 묶음'으로 보고 그 전체에 대해 한 번만 보정해야 한다 — 호출부가
    "이번에 비교하는 검정 전체"를 한 리스트로 모아서 넘겨야 한다."""
    p_values = {id(r): r.p_value for r in results}
    q = benjamini_hochberg(p_values)
    for r in results:
        r.q_value = q[id(r)]


def _summarize(
    returns: pd.Series,
    event_returns: pd.Series,
    window_all_returns: pd.Series,
    direction: str,
    horizon: int,
    min_events: int,
    n_boot: int,
    bootstrap_seed: int,
) -> SignalPerformance:
    n, n_events = len(returns), len(event_returns)
    if n:
        mean_r, median_r, worst = float(returns.mean()), float(returns.median()), float(returns.min())
        hit_rate = float(_hits(returns, direction).mean() * 100)
    else:
        mean_r = median_r = worst = hit_rate = float("nan")

    base_pct = base_rate(window_all_returns, direction)

    if n_events:
        event_hits = _hits(event_returns, direction)
        hit_rate_events = float(event_hits.mean() * 100)
        edge_pct = hit_rate_events - base_pct if base_pct == base_pct else float("nan")
        p_value = binomial_p_value(n_events, int(event_hits.sum()), base_pct / 100 if base_pct == base_pct else float("nan"))
        ci_lower, ci_upper = bootstrap_ci(event_returns, direction, n_boot=n_boot, random_state=bootstrap_seed)
    else:
        hit_rate_events = edge_pct = p_value = ci_lower = ci_upper = float("nan")

    significant = bool(
        n_events >= min_events and ci_lower == ci_lower and base_pct == base_pct and ci_lower > base_pct
    )

    return SignalPerformance(
        direction=direction, horizon=horizon, n=n, hit_rate_pct=hit_rate,
        mean_return_pct=mean_r, median_return_pct=median_r, worst_pct=worst,
        reliable=n >= MIN_SAMPLES, n_events=n_events, hit_rate_pct_events=hit_rate_events,
        base_rate_pct=base_pct, edge_pct=edge_pct, p_value=p_value,
        ci_lower_pct=ci_lower, ci_upper_pct=ci_upper, significant=significant,
    )


def evaluate_signal(
    is_on: pd.Series,
    close: pd.Series,
    direction: str,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    out_of_sample_years: int = OUT_OF_SAMPLE_YEARS,
    min_events: int = DEFAULT_MIN_EVENTS,
    n_boot: int = DEFAULT_N_BOOT,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[int, dict[str, SignalPerformance]]:
    """
    horizon(거래일)별로 {'full', 'in_sample', 'out_of_sample'} 세 구간의
    SignalPerformance를 낸다. out_of_sample은 달력 기준 최근
    out_of_sample_years년(analysis/price_rule_backtest.py와 같은 분할 방식).

    min_events/n_boot/bootstrap_seed는 통계 검증(기준률 대비 유의성) 계산에
    쓰인다 — 모듈 docstring의 "통계 검증" 섹션 참고.
    """
    close = close.sort_index()
    is_on = is_on.reindex(close.index).fillna(0).astype(int)
    event_start = cluster_events(is_on)

    cutoff = close.index[-1] - pd.DateOffset(years=out_of_sample_years)
    windows = {
        "full": close.index,
        "in_sample": close.index[close.index < cutoff],
        "out_of_sample": close.index[close.index >= cutoff],
    }

    out: dict[int, dict[str, SignalPerformance]] = {}
    for horizon in horizons:
        fwd = forward_returns(close, horizon)
        per_window = {}
        for wname, widx in windows.items():
            valid = fwd.loc[widx].notna()
            on_dates = widx[(is_on.loc[widx] == 1) & valid]
            event_dates = widx[event_start.loc[widx] & valid]
            per_window[wname] = _summarize(
                fwd.loc[on_dates], fwd.loc[event_dates], fwd.loc[widx],
                direction, horizon, min_events, n_boot, bootstrap_seed,
            )
        out[horizon] = per_window
    return out
