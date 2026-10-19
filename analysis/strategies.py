"""
기술적 지표 기반 보유 규칙.

analysis/indicators.py처럼 순수 함수. DB도 API도 모른다.
각 함수는 "보유 여부" 시리즈(1=보유, 0=현금)를 종가 Series와 같은 인덱스로 돌려준다.
전부 롱 전용이다 — 공매도(음수 보유)는 만들지 않는다.

주의(미래 참조 금지는 여기가 아니라 체결 시점에서 지킨다): 이 함수들이 돌려주는
신호는 '그 날 종가로 계산 가능한 신호'다. 그 신호가 실제로 체결되는 시점(다음
거래일 시가)은 analysis/rule_backtest.py가 책임진다. 이 모듈은 체결을 모른다.

파라미터 최적화는 이번 단계에서 하지 않는다. 함수 인자로 전부 노출해두지만
기본값만 쓴다 — 적은 데이터로 파라미터를 들여다보며 맞추면 과거에만 맞는
우연한 조합(과최적화/curve-fitting)을 "전략"이라고 착각하기 쉽기 때문이다.
"""

from __future__ import annotations

import pandas as pd

from analysis import indicators as ind

DEFAULT_ENSEMBLE_THRESHOLD = 0.5  # 4개 중 2개 이상 보유 신호


def ma_cross(s: pd.Series, fast: int = 5, slow: int = 20) -> pd.Series:
    """SMA(fast) > SMA(slow)면 보유. 둘 중 하나라도 계산 불가(NaN)면 현금."""
    sma_fast = ind.sma(s, fast)
    sma_slow = ind.sma(s, slow)
    return (sma_fast > sma_slow).astype(int)


def rsi_reversion(s: pd.Series, n: int = 14, lower: float = 30, upper: float = 70) -> pd.Series:
    """RSI < lower면 진입, RSI > upper면 청산. 그 사이는 직전 상태를 유지한다."""
    r = ind.rsi(s, n)
    signal = pd.Series(index=s.sort_index().index, dtype=float)
    signal[r < lower] = 1.0
    signal[r > upper] = 0.0
    return signal.ffill().fillna(0).astype(int)


def bollinger_reversion(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    """종가 < 하단밴드면 진입, 종가 > 중간밴드면 청산. 그 사이는 직전 상태 유지."""
    s = s.sort_index()
    middle, _upper, lower = ind.bollinger(s, n, k)
    signal = pd.Series(index=s.index, dtype=float)
    signal[s < lower] = 1.0
    signal[s > middle] = 0.0
    return signal.ffill().fillna(0).astype(int)


def macd_cross(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """MACD선 > 시그널선이면 보유."""
    macd_line, signal_line = ind.macd(s, fast, slow, signal)
    return (macd_line > signal_line).astype(int)


def ensemble(
    s: pd.Series,
    threshold: float = DEFAULT_ENSEMBLE_THRESHOLD,
    ma_kwargs: dict | None = None,
    rsi_kwargs: dict | None = None,
    bollinger_kwargs: dict | None = None,
    macd_kwargs: dict | None = None,
) -> pd.Series:
    """
    4개 규칙의 보유 여부를 평균낸 점수(0~1)가 threshold 이상이면 보유.

    threshold=0.5면 4개 중 2개 이상 동의해야 보유 — 단일 규칙의 과민반응을
    누르는 목적. threshold=0.75면 3개 이상 동의해야 해서 더 보수적이다.
    둘을 나란히 비교하는 건 scripts/backtest_strategies.py에서 한다(이 함수는
    threshold 하나만 받는다).
    """
    votes = pd.concat(
        [
            ma_cross(s, **(ma_kwargs or {})),
            rsi_reversion(s, **(rsi_kwargs or {})),
            bollinger_reversion(s, **(bollinger_kwargs or {})),
            macd_cross(s, **(macd_kwargs or {})),
        ],
        axis=1,
    )
    score = votes.mean(axis=1)
    return (score >= threshold).astype(int)
