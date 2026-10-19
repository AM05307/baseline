"""
캘린더 기반 이벤트 규칙.

이 모듈은 '예측'을 하지 않는다. 날짜에 해당하는 사실만 반환한다.
외부 API나 DB에 의존하지 않는 순수 함수 모음.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Event:
    """특정 날짜에 해당하는 사실. 방향성 판단은 담지 않는다."""

    kind: str  # "expiry" | "period" | "earnings"
    label: str
    note: str
    date: date


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """해당 월의 n번째 특정 요일. weekday: 월=0 ... 일=6"""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def us_option_expiry(year: int, month: int) -> date:
    """미국 월간 옵션 만기일 = 셋째 금요일."""
    return _nth_weekday(year, month, weekday=4, n=3)


def kr_option_expiry(year: int, month: int) -> date:
    """한국 옵션 만기일 = 둘째 목요일."""
    return _nth_weekday(year, month, weekday=3, n=2)


def is_quadruple_witching(d: date) -> bool:
    """네 마녀의 날: 3·6·9·12월 미국 옵션 만기일."""
    return d.month in (3, 6, 9, 12) and d == us_option_expiry(d.year, d.month)


# 수동 등록 이벤트. 공시·발표로 확정된 사실만 넣는다.
# 추정이나 전망은 여기 넣지 않는다.
PERIODS: list[dict] = [
    {
        "label": "SK하이닉스 자사주 매입 기간",
        "start": date(2026, 8, 20),
        "end": date(2026, 11, 19),
        "note": "2026-08-19 이사회 결의, 장내매수 진행 (DART 공시)",
    },
]

EARNINGS: list[dict] = [
    {"label": "엔비디아 실적 발표", "date": date(2026, 8, 26), "note": "미국 장 마감 후"},
]

# 가중치 없이 "주시할 이벤트"로만 표시(원칙 1·3·4의 신호 체계와 무관 — 날짜가
# 불확실하거나(FOMC/금통위 연내 일정 등) 반복적(실적 시즌)이라 특정일 트리거로
# 못 넣는다. 지어낸 날짜를 넣지 않고, 아는 만큼만 적는다). 사용자가 전달한
# 내용을 그대로 옮긴 것 — 전망이 아니라 "뭘 주시하고 있는지"다.
THEMATIC_WATCHLIST: list[dict] = [
    {"label": "하이퍼스케일러(메타·MS·구글·아마존) 실적 발표 주간", "expert_id": "KHJ",
     "note": "작은 뉴스에도 출렁임 주의"},
    {"label": "미국 FOMC, 한국 금통위", "expert_id": "KSJ",
     "note": "한국은 추가 인상 2~3회 예상이라는 의견(KSJ)"},
    {"label": "OpenAI 상장 일정(내년 초 연기) 및 대규모 리파이낸싱, Anthropic 상장 준비",
     "expert_id": "KHJ·YJH", "note": None},
    {"label": "미국 중간선거", "expert_id": "YJH", "note": "데이터센터 규제·착공 지연 리스크"},
    {"label": "분기 말·반기 말 리밸런싱, 네 마녀의 날", "expert_id": None,
     "note": "기존 캘린더(is_quadruple_witching 등)로 이미 추적 중"},
]


def events_for(d: date) -> list[Event]:
    """해당 날짜에 걸리는 이벤트 목록."""
    out: list[Event] = []

    if d == us_option_expiry(d.year, d.month):
        if is_quadruple_witching(d):
            out.append(Event("expiry", "네 마녀의 날",
                             "미국 지수선물·지수옵션·개별주식옵션 동시 만기", d))
        else:
            out.append(Event("expiry", "미국 옵션 만기일", "월간 옵션 만기", d))

    if d == kr_option_expiry(d.year, d.month):
        out.append(Event("expiry", "한국 옵션 만기일", "국내 월간 옵션 만기", d))

    for p in PERIODS:
        if p["start"] <= d <= p["end"]:
            out.append(Event("period", p["label"], p["note"], d))

    for e in EARNINGS:
        delta = (e["date"] - d).days
        if 0 <= delta <= 7:
            tag = "당일" if delta == 0 else f"D-{delta}"
            out.append(Event("earnings", f"{e['label']} ({tag})", e["note"], e["date"]))

    return out


def historical_quad_witching(start: date, end: date) -> list[date]:
    """
    [start, end] 구간의 과거 네 마녀의 날 목록.

    upcoming()/events_for()는 미래 판단용이라 과거 이벤트를 못 돌려준다.
    백테스트(web 대시보드, MCP 서버 둘 다)는 과거 이벤트가 필요해서 분리했다.
    규칙은 위 us_option_expiry / is_quadruple_witching 그대로 재사용한다 —
    여기서 새 규칙을 만들지 않는다.
    """
    dates: list[date] = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        if m in (3, 6, 9, 12):
            d = us_option_expiry(y, m)
            if start <= d <= end:
                dates.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return dates


def upcoming(d: date, days: int = 30) -> list[Event]:
    """
    앞으로 N일 안의 이벤트를 날짜순으로. 각 이벤트는 한 번만 나온다.

    기간형(period)은 시작일 기준 1회, 실적(earnings)은 발표일 1회.
    만기일은 해당 날짜마다 1회.
    """
    end = d + timedelta(days=days)
    out: list[Event] = []

    # 만기일: 이번 달부터 필요한 개월 수만큼 확인
    cur = date(d.year, d.month, 1)
    while cur <= end:
        for fn, base in ((us_option_expiry, "미국"), (kr_option_expiry, "한국")):
            ex = fn(cur.year, cur.month)
            if d <= ex <= end:
                if base == "미국" and is_quadruple_witching(ex):
                    out.append(Event("expiry", "네 마녀의 날",
                                     "미국 지수선물·지수옵션·개별주식옵션 동시 만기", ex))
                else:
                    out.append(Event("expiry", f"{base} 옵션 만기일", "월간 옵션 만기", ex))
        cur = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)

    # 기간형: 겹치면 1회만
    for p in PERIODS:
        if p["start"] <= end and p["end"] >= d:
            out.append(Event("period", p["label"],
                             f"{p['start']} ~ {p['end']} · {p['note']}",
                             max(p["start"], d)))

    # 실적: 발표일 1회
    for e in EARNINGS:
        if d <= e["date"] <= end:
            out.append(Event("earnings", e["label"], e["note"], e["date"]))

    return sorted(out, key=lambda e: e.date)


if __name__ == "__main__":
    today = date.today()
    print(f"=== {today} 해당 이벤트 ===")
    for ev in events_for(today) or []:
        print(f"  [{ev.kind}] {ev.label} — {ev.note}")
    print(f"\n=== 향후 60일 ===")
    for ev in upcoming(today, 60):
        print(f"  {ev.date} [{ev.kind}] {ev.label}")
