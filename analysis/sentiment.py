"""
VIX 기반 시장 심리 게이지.

CNN Fear & Greed Index는 여러 비공개 지표를 조합한 독자 지수라 무료 API가
없다. 대신 CBOE가 공식 발표하는 VIX(변동성지수) 하나만으로, 방법론을
투명하게 공개할 수 있는 대체 게이지를 만든다. CNN 지수와 수치가 다를 수
있고, 다른 게 당연하다 — 같은 지수를 복제한 게 아니다.

방법론 (전부 여기 적은 게 전부다):
  1. 최근 window(기본 252거래일 ≈ 1년)간의 VIX 분포에서 오늘 VIX의 백분위를 구한다.
  2. VIX가 높을수록 공포이므로, 게이지 점수 = 100 - 백분위로 뒤집는다.
     (VIX가 최근 1년 중 상위 10%에 있으면 → 게이지 점수 10점 → "극단적 공포")
  3. 점수를 5구간(각 20점)으로 나눠 라벨을 붙인다.

이건 "오늘 VIX가 최근 분포에서 어디 있는가"라는 서술이지, "그러니 내일
오를 것이다/떨어질 것이다"가 아니다 — 그 경계를 넘지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

MIN_SAMPLES = 60  # percentile 계산에 최소 이 정도는 있어야 의미가 있다고 봄

BANDS = [
    (0, 20, "극단적 공포 (Extreme Fear)"),
    (20, 40, "공포 (Fear)"),
    (40, 60, "중립 (Neutral)"),
    (60, 80, "탐욕 (Greed)"),
    (80, 100, "극단적 탐욕 (Extreme Greed)"),
]


@dataclass
class SentimentGauge:
    vix_level: float
    percentile: float      # VIX가 최근 window 중 몇 백분위인가 (높을수록 VIX가 높음=공포)
    score: float            # 100 - percentile. 게이지에 그대로 쓰는 값 (높을수록 탐욕)
    category: str
    samples: int
    reliable: bool

    def __str__(self) -> str:
        flag = "" if self.reliable else "  ⚠ 표본 부족"
        return f"VIX {self.vix_level:.1f} · {self.category} (게이지 {self.score:.0f}/100, n={self.samples}){flag}"


def _category(score: float) -> str:
    for lo, hi, label in BANDS:
        if lo <= score <= hi:
            return label
    return "중립 (Neutral)"


def vix_gauge(vix: pd.Series, window: int = 252) -> SentimentGauge:
    """
    vix: VIX 종가 시계열 (index=날짜, 오름차순이 아니어도 됨 — 정렬해서 씀).
    """
    s = vix.sort_index().dropna().tail(window)
    if s.empty:
        return SentimentGauge(float("nan"), float("nan"), float("nan"), "데이터 없음", 0, False)

    latest = float(s.iloc[-1])
    n = len(s)
    percentile = float((s <= latest).sum()) / n * 100
    score = 100 - percentile
    return SentimentGauge(
        vix_level=latest,
        percentile=round(percentile, 1),
        score=round(score, 1),
        category=_category(score),
        samples=n,
        reliable=n >= MIN_SAMPLES,
    )
