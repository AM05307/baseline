"""
ADR(미국예탁증권) 프리미엄 계산.

SK하이닉스 ADR(SKHY, 나스닥)과 KRX 보통주(000660) 사이의 가격 차이를 측정한다.
'프리미엄이 크다/작다'는 관측값이고, 방향을 예측하지 않는다 — 프리미엄이
언제 좁혀지거나 벌어질지는 이 모듈이 판단하지 않는다.

전환비율: 1 ADS = 000660 보통주 1/10주 (10 ADS = 보통주 1주).
출처: SK hynix ADR 프리미엄 추적 사이트(skhypremium.com)의 계산식 설명 및
관련 보도(2026년 나스닥 상장). 공식 예탁계약서(F-1/depositary agreement)로
재확인 전까지는 이 값을 하드코딩하지 않고 인자로 받는다 — 숫자가 잘못되면
프리미엄 전체가 틀어지기 때문에, 틀렸을 때 코드 한 곳만 고치면 되게 했다.

패리티(parity, 이론가) 계산:
    패리티(ADS, USD) = KRX 종가(KRW) / (USD/KRW 환율) / 전환비율
    프리미엄(%) = SKHY 실제가(USD) / 패리티 - 1

이 모듈은 analysis/ 관례대로 순수 함수만 담는다 — DB도 API도 모른다.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RATIO = 10  # 10 ADS = 보통주 1주. 재확인 필요 (모듈 docstring 참고)


@dataclass
class AdrPremium:
    parity_usd: float        # 이론가 (KRX 가격을 ADS 단위로 환산한 값)
    actual_usd: float        # SKHY 실제 종가
    premium_pct: float       # (actual/parity - 1) * 100
    ratio: int

    def __str__(self) -> str:
        return f"ADR 프리미엄 {self.premium_pct:+.2f}% (패리티 ${self.parity_usd:.2f} vs 실제 ${self.actual_usd:.2f})"


def parity_value(krx_close_krw: float, usd_krw_rate: float, ratio: int = DEFAULT_RATIO) -> float:
    """KRX 종가(원)를 ADS 1주의 이론적 달러 가치로 환산."""
    if usd_krw_rate <= 0 or ratio <= 0:
        return float("nan")
    return krx_close_krw / usd_krw_rate / ratio


def premium(
    krx_close_krw: float, adr_close_usd: float, usd_krw_rate: float,
    ratio: int = DEFAULT_RATIO,
) -> AdrPremium:
    """오늘자 프리미엄 하나를 계산. 관측값 — 다음날 값을 예측하지 않는다."""
    parity = parity_value(krx_close_krw, usd_krw_rate, ratio)
    pct = (adr_close_usd / parity - 1) * 100 if parity == parity and parity != 0 else float("nan")
    return AdrPremium(parity_usd=parity, actual_usd=adr_close_usd, premium_pct=pct, ratio=ratio)


def premium_series(krx: "pd.Series", adr: "pd.Series", fx: "pd.Series",  # noqa: F821
                    ratio: int = DEFAULT_RATIO):
    """
    세 시계열(KRX 종가, ADR 종가, 환율)의 공통 날짜에 대해 프리미엄(%) 시계열을 만든다.
    지연 import로 pandas 의존을 함수 안에 가둬서, 이 모듈을 가볍게 훑어볼 때
    타입 힌트 때문에 pandas가 강제로 필요하지 않게 했다.
    """
    import pandas as pd

    joined = pd.concat(
        [krx.rename("krx"), adr.rename("adr"), fx.rename("fx")], axis=1, join="inner"
    ).dropna()
    if joined.empty:
        return pd.Series(dtype=float)
    parity = joined["krx"] / joined["fx"] / ratio
    return (joined["adr"] / parity - 1) * 100
