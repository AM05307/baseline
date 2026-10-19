"""
날짜별 KRX 가격제한(상/하한 %) 테이블 + 일일 수익률이 그 시점 제한을
넘는지 판정하는 순수 함수(6단계: "날짜별 가격제한 테이블 기준으로 전체
티커의 이상치를 스캔").

**analysis/adr.py의 전환비율과 같은 신뢰 수준으로 다룰 것** — 공식 KRX
공시로 재확인 전까지는 참고용이다. 지금은 **2015-06-15 ±30% 개편**(가장
최근 변경, 비교적 널리 알려진 날짜) 하나만 넣었다. 그 이전 연혁(6%→8%→
12%→15%로 단계적으로 바뀌어온 것으로 알려져 있으나, 정확한 시행일을 이
세션에서 공식 출처로 확인하지 못했다)은 **지어내지 않고 비워뒀다** —
analysis/trading_costs.py의 TAX_RATE_SCHEDULE과 같은 이유·같은 패턴
(값을 하나만 확신 있게 넣고, 나머지는 TODO로 명시).

그래서 이 스케줄에 없는(2015-06-15 이전) 날짜는 limit_pct_on()이 None을
돌려주고, find_exceedances()는 그런 날짜를 "위반 아님"이 아니라 "판정
불가"로 건너뛴다 — 모르는 걸 위반이라고 하지도, 정상이라고 하지도 않는다.

analysis/ 관례대로 DB/API를 모른다 — 호출부(scripts/verify_outlier.py)가
db.store에서 가격을 읽어 이 모듈의 입력 모양으로 바꿔준다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class PriceLimitChange:
    effective_on: date
    limit_pct: float  # 상하한 %(예: 30.0 = ±30%)
    source_note: str


PRICE_LIMIT_SCHEDULE: list[PriceLimitChange] = [
    PriceLimitChange(
        effective_on=date(2015, 6, 15), limit_pct=30.0,
        source_note=(
            "2015-06-15 KRX 가격제한 ±15% -> ±30% 개편. 비교적 널리 알려진 "
            "날짜지만 공식 KRX 공시로 재확인 전까지는 참고용이다. 그 이전 "
            "연혁(6%/8%/12%/15% 단계로 바뀌어온 것으로 알려져 있으나 정확한 "
            "시행일을 이 세션에서 확인하지 못해 비워둠). TODO: 공식 출처로 "
            "확인되는 대로 이 리스트에 PriceLimitChange를 추가할 것."
        ),
    ),
]


def limit_pct_on(d: date, schedule: list[PriceLimitChange] | None = None) -> float | None:
    """d 시점에 적용되던 가격제한(%). 스케줄에 없는(더 이전) 날짜는 None —
    지어내지 않는다."""
    schedule = schedule if schedule is not None else PRICE_LIMIT_SCHEDULE
    applicable = [c for c in schedule if c.effective_on <= d]
    if not applicable:
        return None
    return max(applicable, key=lambda c: c.effective_on).limit_pct


@dataclass
class LimitExceedance:
    ticker: str
    date: str
    return_pct: float
    limit_pct: float


def find_exceedances(
    closes_by_ticker: dict[str, pd.Series],
    schedule: list[PriceLimitChange] | None = None,
    tolerance_pct: float = 0.5,
) -> list[LimitExceedance]:
    """
    각 티커의 전일 종가 대비 일일 수익률이, 그 날짜에 적용되던 가격제한을
    tolerance_pct(여유분, 기본 0.5%p — 등락폭 계산 기준 미세한 차이를
    고려한 허용오차)보다 더 넘으면 기록한다.

    기준을 모르는 날짜(schedule에 없는 구간)는 판정하지 않고 건너뛴다.
    ETF·지수(예: 코스닥 지수, VNM 등)처럼 가격제한 자체가 다르거나 없는
    티커가 섞여 있어도 이 함수는 구분하지 않는다 — 호출부가 KRX 개별
    종목만 넘기도록 책임진다(이 함수는 "계산"만, 무엇을 넘길지는 호출부
    판단).
    """
    schedule = schedule if schedule is not None else PRICE_LIMIT_SCHEDULE
    out: list[LimitExceedance] = []
    for ticker, closes in closes_by_ticker.items():
        s = closes.sort_index()
        ret = (s.pct_change() * 100).dropna()
        for dt, r in ret.items():
            d = dt.date() if hasattr(dt, "date") else dt
            limit = limit_pct_on(d, schedule)
            if limit is None:
                continue
            if abs(float(r)) > limit + tolerance_pct:
                out.append(LimitExceedance(ticker=ticker, date=str(d), return_pct=float(r), limit_pct=limit))
    return out
