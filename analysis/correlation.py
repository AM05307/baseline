"""
종목 간 관계 측정.

전부 순수 함수. DB도 API도 모른다. pandas Series만 받아서 숫자를 돌려준다.
'예측'이 아니라 '측정'이다. 결과에 방향성 해석을 붙이지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MIN_SAMPLES = 30  # 이보다 적으면 신뢰하기 어렵다


@dataclass
class CorrResult:
    coefficient: float
    samples: int
    window: int
    reliable: bool  # 표본이 충분한가

    def __str__(self) -> str:
        flag = "" if self.reliable else "  ⚠ 표본 부족"
        return f"상관계수 {self.coefficient:+.3f} (n={self.samples}){flag}"


def to_returns(prices: pd.Series) -> pd.Series:
    """종가 시계열 → 일간 수익률. 가격 자체가 아니라 변화율로 비교해야 한다."""
    return prices.sort_index().pct_change().dropna()


def correlation(a: pd.Series, b: pd.Series, window: int = 60) -> CorrResult:
    """두 종목 수익률의 상관계수. 공통 날짜만 사용."""
    ra, rb = to_returns(a), to_returns(b)
    joined = pd.concat([ra, rb], axis=1, join="inner").dropna().tail(window)
    n = len(joined)
    if n < 2:
        return CorrResult(float("nan"), n, window, False)
    coef = joined.iloc[:, 0].corr(joined.iloc[:, 1])
    return CorrResult(float(coef), n, window, n >= MIN_SAMPLES)


def lagged_correlation(
    leader: pd.Series, follower: pd.Series, max_lag: int = 3, window: int = 60
) -> dict[int, CorrResult]:
    """
    선행-후행 관계 측정.

    lag=1 이면 '어제의 leader'와 '오늘의 follower'를 비교한다.
    미국장이 한국장에 선행하는지 확인할 때 쓴다.
    """
    rl, rf = to_returns(leader), to_returns(follower)
    out: dict[int, CorrResult] = {}
    for lag in range(max_lag + 1):
        shifted = rl.shift(lag)
        joined = pd.concat([shifted, rf], axis=1, join="inner").dropna().tail(window)
        n = len(joined)
        coef = joined.iloc[:, 0].corr(joined.iloc[:, 1]) if n >= 2 else float("nan")
        out[lag] = CorrResult(float(coef), n, window, n >= MIN_SAMPLES)
    return out


def beta(target: pd.Series, benchmark: pd.Series, window: int = 60) -> float:
    """벤치마크가 1% 움직일 때 대상이 평균 몇 % 움직이는가."""
    rt, rb = to_returns(target), to_returns(benchmark)
    joined = pd.concat([rt, rb], axis=1, join="inner").dropna().tail(window)
    if len(joined) < 2:
        return float("nan")
    cov = joined.iloc[:, 0].cov(joined.iloc[:, 1])
    var = joined.iloc[:, 1].var()
    return float(cov / var) if var else float("nan")


def divergence(
    target: pd.Series, benchmark: pd.Series, window: int = 60, threshold: float = 2.0
) -> pd.DataFrame:
    """
    평소 관계에서 벗어난 날 찾기.

    베타로 예상한 움직임과 실제 움직임의 차이가 표준편차의 threshold배를
    넘으면 '개별 재료가 있었던 날'로 본다. 원인은 알려주지 않는다.
    """
    b = beta(target, benchmark, window)
    rt, rb = to_returns(target), to_returns(benchmark)
    joined = pd.concat([rt, rb], axis=1, join="inner").dropna().tail(window)
    joined.columns = ["target", "benchmark"]
    joined["expected"] = joined["benchmark"] * b
    joined["gap"] = joined["target"] - joined["expected"]
    sd = joined["gap"].std()
    if not sd:
        return joined.iloc[0:0]
    joined["z"] = joined["gap"] / sd
    return joined[joined["z"].abs() >= threshold].sort_values("z")


def summarize(target: pd.Series, benchmark: pd.Series, window: int = 60) -> str:
    """사람이 읽을 요약. 전망은 쓰지 않는다."""
    c = correlation(target, benchmark, window)
    b = beta(target, benchmark, window)
    lags = lagged_correlation(benchmark, target, 2, window)
    best = max(lags.items(), key=lambda kv: abs(kv[1].coefficient) if not np.isnan(kv[1].coefficient) else -1)
    lines = [
        f"· {c}",
        f"· 베타 {b:+.2f} (벤치마크 1% 대비)",
        f"· 최대 상관 시차: {best[0]}일 ({best[1].coefficient:+.3f})",
    ]
    if not c.reliable:
        lines.append(f"· ⚠ 표본 {c.samples}개는 해석에 충분하지 않음 (권장 {MIN_SAMPLES}+)")
    return "\n".join(lines)
