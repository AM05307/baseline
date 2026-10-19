"""
규칙 기반 매수/목표/손절 기준가 산출.

CLAUDE.md 원칙 1(갱신판): "백테스트로 검증된 규칙만 매수/매도 기준가로
표시한다." 여기 함수들은 그 "규칙"을 기계적으로 계산만 한다 — 가격을
예측하지 않는다. 과거에 이 레벨들이 실제로 돈을 벌었는지는
analysis/price_rule_backtest.py가 따로 검증하고, **검증을 통과하지 못한
규칙은 화면에 가격을 노출하지 않는다**(web/main.py에서 걸러낸다).

전부 순수 함수. DB도 API도 모른다. 주문/매매 실행 코드는 없다(원칙 2).

핵심 규칙(analysis/pullback.py·rule_backtest.py와 동일한 전제, 미래 참조
금지): 각 함수가 반환하는 entry_price[t]/target_price[t]/stop_price[t]는
**t일 종가까지의 데이터로 계산한 레벨**이고, 실제 주문은 t+1일에 나간다
(이 시프트는 함수가 아니라 analysis/price_rule_backtest.py의 백테스트
엔진이 일괄 적용한다 — 함수는 그 날의 레벨만 계산).

파라미터는 전부 함수 인자로 노출하되 기본값만 쓴다 — 과최적화 방지
(analysis/strategies.py와 같은 이유).
"""

from __future__ import annotations

import pandas as pd

from analysis import indicators as ind

DEFAULT_MA_PULLBACK = {
    "trend_window": 60, "pullback_window": 20, "target_pct": 0.05, "stop_pct": 0.03,
}
DEFAULT_ATR_BAND = {
    "atr_window": 14, "entry_k": 1.5, "target_k": 2.0, "stop_k": 1.0,
}
DEFAULT_BREAKOUT_RANGE = {
    "target_pct": 0.05,
}


def ma_pullback_levels(
    ohlc: pd.DataFrame,
    trend_window: int = DEFAULT_MA_PULLBACK["trend_window"],
    pullback_window: int = DEFAULT_MA_PULLBACK["pullback_window"],
    target_pct: float = DEFAULT_MA_PULLBACK["target_pct"],
    stop_pct: float = DEFAULT_MA_PULLBACK["stop_pct"],
) -> pd.DataFrame:
    """
    이동평균 눌림목: 종가가 장기 이동평균(trend_window) 위에 있는(상승
    추세) 동안, 더 짧은 이동평균(pullback_window)까지 되돌림이 오면 그
    지점에 지정가 매수를 건다고 가정. 추세 조건이 안 맞으면 그날은
    신호 없음(NaN) — 추세와 무관하게 아무 날에나 사지 않는다.

    entry = SMA(pullback_window), target = entry*(1+target_pct),
    stop = entry*(1-stop_pct).
    """
    close = ohlc["close"].sort_index()
    trend_ma = ind.sma(close, trend_window)
    pullback_ma = ind.sma(close, pullback_window)
    eligible = close > trend_ma
    entry = pullback_ma.where(eligible)
    return pd.DataFrame({
        "entry_price": entry,
        "target_price": entry * (1 + target_pct),
        "stop_price": entry * (1 - stop_pct),
    })


def atr_band_levels(
    ohlc: pd.DataFrame,
    atr_window: int = DEFAULT_ATR_BAND["atr_window"],
    entry_k: float = DEFAULT_ATR_BAND["entry_k"],
    target_k: float = DEFAULT_ATR_BAND["target_k"],
    stop_k: float = DEFAULT_ATR_BAND["stop_k"],
) -> pd.DataFrame:
    """
    ATR 밴드: 당일 종가에서 ATR의 entry_k배만큼 아래에 지정가 매수.
    변동성이 큰 날은 밴드가 넓어지고 작은 날은 좁아진다 — 고정 %
    밴드(ma_pullback_levels의 target_pct/stop_pct 같은)와 다른 점.

    entry = 종가 - entry_k*ATR, target = entry + target_k*ATR,
    stop = entry - stop_k*ATR. ATR이 아직 계산 안 되는 초반 구간은 NaN.
    """
    ohlc = ohlc.sort_index()
    close = ohlc["close"]
    atr = ind.atr(ohlc["high"], ohlc["low"], close, atr_window)
    entry = close - entry_k * atr
    valid = atr.notna()
    return pd.DataFrame({
        "entry_price": entry.where(valid),
        "target_price": (entry + target_k * atr).where(valid),
        "stop_price": (entry - stop_k * atr).where(valid),
    })


def breakout_range_levels(
    ohlc: pd.DataFrame,
    target_pct: float = DEFAULT_BREAKOUT_RANGE["target_pct"],
) -> pd.DataFrame:
    """
    전일 고저 돌파: 당일 고가를 매수 기준가(돌파 매수, buy-stop)로 삼는다.
    손절은 당일 저가 그대로 — 전일 고저 자체가 돌파/손절 레벨이라는 규칙의
    정의상 별도 비율 파라미터가 없다. 목표가만 target_pct로 조절한다.

    entry = 고가, target = entry*(1+target_pct), stop = 저가.
    (ma_pullback_levels·atr_band_levels와 달리 지정가가 아니라 '돌파' 매수
    라서 체결 판정 방식이 다르다 — analysis/price_rule_backtest.py의
    entry_type="stop" 참고.)
    """
    ohlc = ohlc.sort_index()
    high, low = ohlc["high"], ohlc["low"]
    valid = high.notna() & low.notna()
    return pd.DataFrame({
        "entry_price": high.where(valid),
        "target_price": (high * (1 + target_pct)).where(valid),
        "stop_price": low.where(valid),
    })


RULES: dict[str, dict] = {
    "ma_pullback": {
        "label": "이동평균 눌림목", "fn": ma_pullback_levels, "entry_type": "limit",
    },
    "atr_band": {
        "label": "ATR 밴드", "fn": atr_band_levels, "entry_type": "limit",
    },
    "breakout_range": {
        "label": "전일 고저 돌파", "fn": breakout_range_levels, "entry_type": "stop",
    },
}
