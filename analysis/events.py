"""
수급 이벤트(자사주 매입 기간·실적발표일·콘퍼런스콜) — 입력 검증과 DART 공시에서의 자동 추출.

이벤트는 "신호"가 아니라 **기록**이다(원칙 5와 같은 선상): 언제 무슨 일이 있었는지/있을
예정인지를 저장하고, 과거 이벤트 전후의 주가·수급을 analysis/event_study.py가 측정한다.
이 모듈은 어떤 방향성 전망도 만들지 않는다.

순수 함수만 담는다(DB/API 비의존). DART 연동:
  - `extract_buyback_events()`는 **이미 DB에 수집된 disclosures 행**(공시검색 목록)만 본다.
    공시검색 목록에는 접수일·보고서명만 있고 **취득 예정 기간(시작·종료)이 없다** — 기간은
    '자기주식 취득 결정' 상세 공시에만 있다. 그래서 기간을 아는 경우(details 인자로 전달)에만
    이벤트를 만들고, 모르면 `unresolved`에 사유와 함께 남긴다(기간을 지어내지 않음).
  - 상세 조회(`DartClient.treasury_stock_acquisition_decision`)는 키가 없어 한 번도 호출하지
    않았다. 아래 TODO 표시 항목은 키 발급 후 실제 응답으로 확정해야 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

EVENT_TYPES: dict[str, str] = {
    "buyback": "자사주 매입",
    "earnings": "실적발표",
    "conf_call": "콘퍼런스콜",
}
EVENT_SOURCES = ("manual", "dart")

# --- DART (전부 TODO: 공식문서 재확인 — 키가 없어 실제 응답으로 확인하지 못함) ---
# TODO: 공시 보고서명 키워드. 아래는 '주요사항보고서(자기주식 취득 결정)' 계열로 흔히 알려진 표기이며,
#       실제 report_nm 표기(공백·괄호·'[기재정정]' 접두어)는 수집된 disclosures로 확인할 것.
#       '자기주식처분결정'(처분)은 취득이 아니므로 제외하고, '자기주식취득결과보고서'는 실제 취득 종료일을
#       알려줄 수 있지만 상세 필드명을 확인 못해 이번 범위에서 다루지 않는다(TODO).
BUYBACK_DECISION_KEYWORDS = ("자기주식취득결정", "자기주식취득신탁계약체결결정")
# TODO: 상세 응답의 취득 예정 기간 필드명 후보(OpenDART '자기주식 취득 결정' 응답에서 흔히 알려진 이름).
#       확실하지 않아 후보를 여러 개 두고, 하나도 못 찾으면 None(지어내지 않음).
BUYBACK_PERIOD_START_FIELDS = ("aqexpd_bgd", "aq_expd_bgd")
BUYBACK_PERIOD_END_FIELDS = ("aqexpd_edd", "aq_expd_edd")


def normalize_date(value) -> str | None:
    """
    'YYYY-MM-DD'·'YYYYMMDD'·'YYYY.MM.DD'·'2026년 01월 05일' 등을 'YYYY-MM-DD'로. 해석 못 하면 None.
    (DART 상세 응답의 날짜 표기가 일정하지 않을 수 있어 관대하게 받는다 — TODO 공식문서 재확인.)
    """
    if value is None:
        return None
    if isinstance(value, (date, pd.Timestamp)):
        return pd.Timestamp(value).date().isoformat()
    text = str(value).strip()
    m = re.search(r"(\d{4})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})", text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def validate_event(row: dict) -> dict:
    """
    저장 전 검증·정규화. 반환: store.upsert_event 입력 모양.
    - event_type은 EVENT_TYPES 키여야 한다.
    - start_date 필수, end_date 생략 시 start_date(단일 날짜 이벤트).
    - 자사주 매입(buyback)은 **기간 이벤트**라 end_date를 명시해야 한다(생략하면 1일짜리로 오인되므로 거부).
    - end_date < start_date 거부.
    """
    etype = row.get("event_type")
    if etype not in EVENT_TYPES:
        raise ValueError(f"event_type은 {sorted(EVENT_TYPES)} 중 하나여야 합니다: {etype!r}")
    start = normalize_date(row.get("start_date"))
    if start is None:
        raise ValueError(f"start_date를 해석할 수 없습니다: {row.get('start_date')!r}")
    raw_end = row.get("end_date")
    if etype == "buyback" and (raw_end is None or str(raw_end).strip() == ""):
        raise ValueError("자사주 매입(buyback)은 기간 이벤트라 end_date가 필요합니다")
    end = normalize_date(raw_end) if raw_end not in (None, "") else start
    if end is None:
        raise ValueError(f"end_date를 해석할 수 없습니다: {raw_end!r}")
    if end < start:
        raise ValueError(f"end_date({end})가 start_date({start})보다 빠릅니다")
    source = row.get("source") or "manual"
    if source not in EVENT_SOURCES:
        raise ValueError(f"source는 {EVENT_SOURCES} 중 하나여야 합니다: {source!r}")
    ticker = (row.get("ticker") or "").strip()
    if not ticker:
        raise ValueError("ticker가 필요합니다")
    return {
        "ticker": ticker, "event_type": etype, "start_date": start, "end_date": end,
        "title": row.get("title"), "note": row.get("note"),
        "source": source, "source_ref": row.get("source_ref"),
    }


def is_buyback_decision(report_nm: str | None) -> bool:
    """보고서명에서 공백·괄호 안내를 지우고 '자기주식 취득 결정' 계열인지 본다. '처분'은 제외."""
    if not report_nm:
        return False
    compact = re.sub(r"\s+", "", report_nm)
    if "처분" in compact:
        return False
    return any(k in compact for k in BUYBACK_DECISION_KEYWORDS)


def parse_buyback_period(detail: dict) -> tuple[str, str] | None:
    """상세 응답 한 건에서 (시작, 종료) 'YYYY-MM-DD'. 필드가 없거나 해석 불가거나 종료<시작이면 None."""
    def first(fields):
        for f in fields:
            if f in detail:
                d = normalize_date(detail[f])
                if d:
                    return d
        return None

    start, end = first(BUYBACK_PERIOD_START_FIELDS), first(BUYBACK_PERIOD_END_FIELDS)
    if start is None or end is None or end < start:
        return None
    return start, end


@dataclass
class ExtractResult:
    events: list[dict] = field(default_factory=list)       # store.upsert_event 입력 모양
    unresolved: list[dict] = field(default_factory=list)   # {rcept_no, report_nm, rcept_dt, reason}


def extract_buyback_events(
    disclosures: pd.DataFrame, details: dict[str, dict] | None = None,
) -> ExtractResult:
    """
    disclosures: store.get_disclosures() 모양(rcept_no, report_nm, rcept_dt, stock_code ...).
    details: {rcept_no: 상세 응답 dict} — 기간을 아는 공시만 이벤트가 된다. None이면 전부 unresolved.

    같은 (종목, 시작, 종료) 기간은 한 번만 만든다(정정공시가 같은 기간을 다시 낼 수 있음).
    기간을 모르는 공시는 이벤트로 만들지 않고 사유(period_unknown / period_unparseable)와 함께 반환.
    """
    result = ExtractResult()
    if disclosures is None or disclosures.empty:
        return result
    details = details or {}
    seen: set[tuple[str, str, str]] = set()
    for _, row in disclosures.sort_values("rcept_dt").iterrows():
        report_nm = row.get("report_nm")
        if not is_buyback_decision(report_nm):
            continue
        rcept_no = row.get("rcept_no")
        base = {"rcept_no": rcept_no, "report_nm": report_nm, "rcept_dt": row.get("rcept_dt")}
        detail = details.get(rcept_no)
        if detail is None:
            result.unresolved.append({**base, "reason": "period_unknown"})
            continue
        period = parse_buyback_period(detail)
        if period is None:
            result.unresolved.append({**base, "reason": "period_unparseable"})
            continue
        ticker = row.get("stock_code")
        if not ticker or ticker != ticker:
            result.unresolved.append({**base, "reason": "stock_code_missing"})
            continue
        key = (ticker, period[0], period[1])
        if key in seen:
            continue
        seen.add(key)
        result.events.append(validate_event({
            "ticker": ticker, "event_type": "buyback", "start_date": period[0], "end_date": period[1],
            "title": report_nm, "note": f"공시 접수일 {row.get('rcept_dt')}",
            "source": "dart", "source_ref": rcept_no,
        }))
    return result
