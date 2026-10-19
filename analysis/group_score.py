"""
신호 그룹 균등 가중치 위험 점수 + 과거 분포 백분위 단계 기준 (2026-10-19 기본값, 순수 함수).

설계: docs/signal_group_weighting_design.md 6.2절 그룹 구성(사용자 확정). 확정값:
  - 그룹 안 신호는 균등 가중치, 그룹끼리도 균등 가중치
  - 기준 정하는 기간 2005-01-01 ~ 2018-12-31, 검증 기간 2019-01-01 ~ 최신
  - 단계 기준: 기준 기간 위험 점수 분포의 상위 30% / 15% / 5% → 1 / 2 / 3단계
기존 적중률 기반 가중치(analysis/weighting.py + risk_score.compute_risk_score)는 method="weighted" 옵션으로 남는다.

점수 정의(그날 값이 있는 것만 센다 — 값이 없는 신호·그룹은 분모에서 뺀다, 숨기지 않고 coverage로 돌려준다):
  그룹 점수 = 그룹 안에서 그날 값이 있는 신호 중 켜진 비율(0~1)
  위험 점수 = 그날 값이 있는 위험 그룹 점수의 평균 × 100 (0~100)
기회 그룹은 표시용이다(상태 판정에 쓰지 않음 — 2026-10-18 분리 결정 그대로).

단계 기준점(동점 처리): 점수가 이산값이라 같은 값이 많다. 상위 q 기준점 = "기준 기간에서 그 값 이상인 날의 비율이 q 이하가 되는
가장 작은 관측값". 그래서 실제 비율은 q보다 작을 수 있고(동점), 그 실제 비율을 함께 돌려준다. 이 규칙을 만족하는 값이 없으면
(최댓값 하나에 q보다 많은 날이 몰려 있으면) 그 단계는 도달 불가(inf)로 둔다. 기준점은 기준 기간 데이터만으로 정하고, 검증 기간
결과를 보고 바꾸지 않는다.

KHJ_COMBO==3 덮어쓰기(예전 방식의 3단계 경로)는 이 방식에 없다 — COMBO는 R2 그룹의 한 신호일 뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

RISK_GROUPS: dict[str, tuple[str, ...]] = {
    "R1 미국 금리": ("YJH_US10Y", "KSJ_US10Y_HIGH", "KHJ_RATE_STAGE"),
    "R2 글로벌 위험선호": ("KHJ_CONCENTRATION", "KHJ_IPO_WEAK", "KHJ_COMBO"),
    "R3 000660 가격 과열": ("KHJ_OVERHEAT", "YJH_BOX_RANGE_TRIM"),
    "R4 반도체 쏠림": ("KSJ_BREADTH",),
    "R5 메모리 업황": ("YJH_MEMORY_MARGIN",),
    "R6 수급": ("KSJ_FOREIGN_SELL",),
}
OPPORTUNITY_GROUPS: dict[str, tuple[str, ...]] = {
    "O1 급락": ("KHJ_3DAY_SELLOFF", "KSJ_PANIC_DAY"),
    "O2 가격 위치 하단": ("YJH_BOX_RANGE_BUY",),
    "O3 밸류 하단": ("YJH_PBR_FLOOR",),
}

CALIBRATION_START = "2005-01-01"
CALIBRATION_END = "2018-12-31"
VALIDATION_START = "2019-01-01"
STAGE_QUANTILES: tuple[float, float, float] = (0.30, 0.15, 0.05)  # 1·2·3단계 = 기준 기간 상위 30%·15%·5%


def group_scores(is_on: pd.DataFrame, groups: dict[str, tuple[str, ...]],
                 exclude: frozenset[str] | set[str] = frozenset()) -> pd.DataFrame:
    """그룹별 점수(0~1). 그날 값이 있는 신호가 하나도 없는 그룹은 NaN."""
    out = {}
    for g, sids in groups.items():
        cols = [s for s in sids if s in is_on.columns and s not in exclude]
        if not cols:
            out[g] = pd.Series(np.nan, index=is_on.index)
            continue
        sub = is_on[cols].astype(float)
        out[g] = (sub == 1).where(sub.notna()).sum(axis=1, min_count=1) / sub.notna().sum(axis=1).replace(0, np.nan)
    return pd.DataFrame(out, index=is_on.index)


def risk_score_series(is_on: pd.DataFrame, exclude: frozenset[str] | set[str] = frozenset(),
                      groups: dict[str, tuple[str, ...]] = RISK_GROUPS) -> pd.Series:
    """위험 점수(0~100) = 값이 있는 위험 그룹 점수의 평균 × 100. 값이 있는 그룹이 없으면 NaN."""
    return group_scores(is_on, groups, exclude).mean(axis=1, skipna=True) * 100


def opportunity_score_series(is_on: pd.DataFrame, exclude: frozenset[str] | set[str] = frozenset()) -> pd.Series:
    """기회 점수(0~100, 표시용)."""
    return risk_score_series(is_on, exclude, OPPORTUNITY_GROUPS)


@dataclass(frozen=True)
class Thresholds:
    values: tuple[float, ...]  # 1·2·3단계 기준점(점수 이상이면 그 단계). inf = 도달 불가
    quantiles: tuple[float, ...]  # 요청한 상위 비율
    actual_shares: tuple[float, ...]  # 기준 기간에서 실제로 그 기준점 이상이었던 날의 비율(동점 때문에 q 이하)
    n_days: int  # 기준 기간 중 점수가 있는 날 수
    start: str
    end: str


def calibrate_thresholds(scores: pd.Series, start: str = CALIBRATION_START, end: str = CALIBRATION_END,
                         quantiles: tuple[float, ...] = STAGE_QUANTILES) -> Thresholds:
    """기준 기간 점수 분포에서 상위 q 기준점을 정한다(모듈 docstring의 동점 규칙)."""
    cal = scores[(scores.index >= pd.Timestamp(start)) & (scores.index <= pd.Timestamp(end))].dropna()
    if cal.empty:
        raise ValueError(f"기준 기간 {start}~{end}에 점수가 없다")
    vals = np.sort(cal.unique())
    share_ge = {v: float((cal >= v - 1e-12).mean()) for v in vals}
    thr, shares = [], []
    for q in quantiles:
        ok = [v for v in vals if share_ge[v] <= q + 1e-12]
        if ok:
            thr.append(float(ok[0]))
            shares.append(share_ge[ok[0]])
        else:
            thr.append(float("inf"))
            shares.append(0.0)
    return Thresholds(tuple(thr), tuple(quantiles), tuple(shares), int(len(cal)), start, end)


def stage_series(scores: pd.Series, thresholds: Thresholds) -> pd.Series:
    """점수 → 단계(0~3). 점수가 NaN이면 0(값 있는 그룹이 없음 — coverage로 따로 보여준다)."""
    stage = pd.Series(0, index=scores.index, dtype=int)
    for k, v in enumerate(thresholds.values, start=1):
        stage[scores >= v - 1e-12] = k
    return stage


def stage_status(stage: int) -> tuple[str, str | None]:
    """단계 → (신규 매수 상태, 보유 중이라면 분할 매도 문구) — 예전 determine_status()와 같은 모양."""
    if stage <= 0:
        return "매수 가능", None
    return "보류", f"분할 매도 {stage}/3 단계"


def coverage(is_on: pd.DataFrame, exclude: frozenset[str] | set[str] = frozenset(),
             groups: dict[str, tuple[str, ...]] = RISK_GROUPS) -> pd.Series:
    """날짜별 값이 있는 그룹 수(분모) — 화면·리포트에 같이 표시."""
    return group_scores(is_on, groups, exclude).notna().sum(axis=1)
