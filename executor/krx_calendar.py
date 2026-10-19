"""
KRX 거래일 캘린더(config/krx_holidays.yaml) — 순수 함수.

is_trading_day()는 세 값 중 하나를 돌려준다: True(거래일) / False(주말·휴장일) / None(캘린더 범위 밖 — 모름).
모르는 날은 거래일로 가정하지 않는다: 섀도 실행·주문을 하지 않고 "캘린더 범위 밖"으로 기록한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class KrxCalendar:
    covered_from: date
    covered_to: date
    holidays: dict[date, str]

    def is_trading_day(self, d: date) -> bool | None:
        if not (self.covered_from <= d <= self.covered_to):
            return None
        if d.weekday() >= 5:
            return False
        return d not in self.holidays

    def holiday_name(self, d: date) -> str | None:
        if d.weekday() == 5:
            return "토요일"
        if d.weekday() == 6:
            return "일요일"
        return self.holidays.get(d)


def parse_calendar(data: dict) -> KrxCalendar:
    hol = {}
    for h in data.get("holidays", []):
        d = date.fromisoformat(str(h["date"]))
        if d.weekday() >= 5:
            raise ValueError(f"휴장일 목록에는 평일만: {d}")
        hol[d] = h.get("name", "휴장일")
    return KrxCalendar(date.fromisoformat(str(data["covered_from"])), date.fromisoformat(str(data["covered_to"])), hol)


NEXT_YEAR_ALERT_FROM = (12, 1)  # 이 날짜(월, 일) 이후에도 다음 해 목록이 없으면 경고


def next_year_warning(cal: KrxCalendar, today: date) -> str | None:
    """12/1 이후인데 캘린더가 다음 해를 덮지 않으면 경고 문구 — 1월 첫 거래일부터 섀도·주문이 "범위 밖"으로 멈추기 전에 알린다."""
    if (today.month, today.day) < NEXT_YEAR_ALERT_FROM or cal.covered_to.year > today.year:
        return None
    return (f"KRX 휴장일 캘린더가 {cal.covered_to.isoformat()}까지만 있다 — {today.year + 1}년 휴장일 목록을 "
            "config/krx_holidays.yaml에 추가하지 않으면 1월부터 섀도 실행·주문이 \"캘린더 범위 밖\"으로 멈춘다")
