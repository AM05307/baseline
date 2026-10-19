"""
기술적 지표 계산.

analysis/correlation.py와 같은 원칙: 전부 순수 함수. DB도 API도 모른다.
pandas Series(종가, index=날짜)만 받아서 숫자/Series를 돌려준다.
지표 자체는 '예측'이 아니라 과거 가격의 변환일 뿐이다 — 해석은 strategies.py,
신호의 과거 성과 측정은 rule_backtest.py에서 한다.
"""

from __future__ import annotations

import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    """단순이동평균. 처음 n-1개는 NaN(원칙: 데이터 부족한 구간을 숫자로 지어내지 않음)."""
    return s.sort_index().rolling(n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """
    RSI, Wilder 방식(지수평활).

    단순 rolling mean이 아니라 지수평활(ewm, alpha=1/n)을 쓴다 — 오리지널
    Wilder(1978) 정의가 이 방식이고, 대부분의 차트 서비스가 이걸 기본값으로 쓴다.
    평균 손실이 0이면(계속 오르기만 함) RSI=100으로 둔다.
    """
    s = s.sort_index()
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    out[avg_loss == 0] = 100.0
    out[avg_gain.isna() | avg_loss.isna()] = float("nan")
    return out


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저밴드 (중간, 상단, 하단). 중간=SMA(n), 폭=표본표준편차 * k."""
    s = s.sort_index()
    middle = sma(s, n)
    std = s.rolling(n).std()
    upper = middle + k * std
    lower = middle - k * std
    return middle, upper, lower


def macd(
    s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series]:
    """MACD선(단기EMA-장기EMA)과 시그널선(MACD선의 EMA)."""
    s = s.sort_index()
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """
    평균진폭(ATR), Wilder 방식(지수평활) — rsi()와 같은 평활 방식.

    true range = max(고가-저가, |고가-전일종가|, |저가-전일종가|).
    갭(전일 종가와 오늘 시가 사이 괴리)까지 포함해서 "하루에 실제로 움직인
    범위"를 본다 — 단순히 고가-저가만 쓰면 갭을 놓친다.
    """
    high, low, close = (s.sort_index() for s in (high, low, close))
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
