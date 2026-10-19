"""
이벤트 스터디 — 이벤트(예: 자사주 매입 종료일) 전후 N거래일의 수익률과 외국인 수급을 측정한다.

**측정이지 전망이 아니다.** "종료 후 오른다/내린다"를 말하지 않는다. 결과는 과거 이벤트들에서
관측된 값의 요약일 뿐이며, 표본수(n)와 최대손실(가장 나빴던 이벤트의 수익률)을 평균과 같은
비중으로 반환한다(원칙 3·4). n<MIN_SAMPLES면 reliable=False.

정의:
  - 기준일 t = 이벤트 날짜 **이하**의 마지막 거래일(주말·휴일이면 직전 거래일). 이벤트 날짜가 시세
    시작 이전이거나 마지막 시세일보다 뒤(아직 안 일어남)면 제외(n_excluded).
  - before = close[t] / close[t-N] - 1,  after = close[t+N] / close[t] - 1  (거래일 N개 기준).
    각 창은 따로 판정한다 — 아직 N거래일이 안 지난 이벤트는 after에서 빠지고 before에는 들어갈
    수 있다(그래서 창마다 n이 다를 수 있고 둘 다 표시한다).
  - 외국인 수급 = 같은 창 안 N거래일의 순매수 합. **창 안 N거래일 값이 전부 있을 때만** 쓴다(일부만
    있는 합은 불완전해서 제외 — 지어내지 않는다). 단위는 수집 소스 기준(수량/금액 미확정).

순수 함수만 담는다(DB/API 비의존).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

MIN_SAMPLES = 30  # 원칙 3. 다른 analysis 모듈과 같은 기준값, 독립 상수(기존 관행).


@dataclass
class ReturnStats:
    n: int
    mean_pct: float
    median_pct: float
    worst_pct: float          # 최대손실: 이벤트들 중 가장 나빴던 수익률
    best_pct: float
    n_negative: int
    reliable: bool            # n >= MIN_SAMPLES


@dataclass
class FlowStats:
    n: int                    # 창 안 N거래일 수급이 전부 있는 이벤트 수
    mean: float
    median: float
    worst: float              # 가장 큰 순매도(합이 가장 작은 값)
    reliable: bool


@dataclass
class EventStudyResult:
    n_days: int
    n_events: int             # 입력 이벤트 수
    n_excluded: int           # 기준일을 못 잡아 아무 창에도 못 쓴 이벤트(시세 시작 이전 등)
    before: ReturnStats
    after: ReturnStats
    flow_before: FlowStats
    flow_after: FlowStats
    per_event: list[dict] = field(default_factory=list)


def _return_stats(values: list[float]) -> ReturnStats:
    n = len(values)
    if n == 0:
        nan = float("nan")
        return ReturnStats(0, nan, nan, nan, nan, 0, False)
    s = pd.Series(values, dtype=float)
    return ReturnStats(
        n=n, mean_pct=float(s.mean()), median_pct=float(s.median()),
        worst_pct=float(s.min()), best_pct=float(s.max()),
        n_negative=int((s < 0).sum()), reliable=n >= MIN_SAMPLES,
    )


def _flow_stats(values: list[float]) -> FlowStats:
    n = len(values)
    if n == 0:
        nan = float("nan")
        return FlowStats(0, nan, nan, nan, False)
    s = pd.Series(values, dtype=float)
    return FlowStats(n=n, mean=float(s.mean()), median=float(s.median()), worst=float(s.min()),
                     reliable=n >= MIN_SAMPLES)


def event_study(
    close: pd.Series,
    event_dates: list,
    n_days: int = 5,
    foreigner: pd.Series | None = None,
) -> EventStudyResult:
    """
    close: 종가 Series(index=날짜). event_dates: 이벤트 날짜들(문자열/Timestamp). n_days: 창 길이(거래일).
    foreigner: 외국인 순매수 Series(index=날짜) 또는 None/빈 Series(= 수급 데이터 없음).
    """
    if n_days < 1:
        raise ValueError("n_days must be >= 1")
    close = close.dropna().sort_index()
    idx = close.index
    has_flow = foreigner is not None and not foreigner.dropna().empty
    flow = foreigner.dropna().sort_index() if has_flow else None

    before_r: list[float] = []
    after_r: list[float] = []
    before_f: list[float] = []
    after_f: list[float] = []
    per_event: list[dict] = []
    excluded = 0

    for raw in event_dates:
        ev = pd.Timestamp(raw)
        if len(idx) == 0 or ev > idx[-1]:
            # 마지막 시세일보다 뒤 = 아직 일어나지 않은(또는 시세가 아직 안 들어온) 이벤트.
            # 마지막 거래일에 갖다 붙이면 미래 이벤트가 과거 표본으로 섞이므로 제외한다.
            excluded += 1
            continue
        pos = idx.searchsorted(ev, side="right") - 1  # ev 이하의 마지막 거래일 위치
        if pos < 0:
            excluded += 1
            continue
        t = idx[pos]
        rec = {"event_date": ev.date().isoformat(), "ref_day": t.date().isoformat(),
               "before_pct": None, "after_pct": None, "foreign_before": None, "foreign_after": None}

        if pos - n_days >= 0:
            r = (close.iloc[pos] / close.iloc[pos - n_days] - 1) * 100
            before_r.append(float(r))
            rec["before_pct"] = float(r)
            if flow is not None:
                win = idx[pos - n_days + 1: pos + 1]
                vals = flow.reindex(win)
                if vals.notna().all():
                    before_f.append(float(vals.sum()))
                    rec["foreign_before"] = float(vals.sum())
        if pos + n_days < len(idx):
            r = (close.iloc[pos + n_days] / close.iloc[pos] - 1) * 100
            after_r.append(float(r))
            rec["after_pct"] = float(r)
            if flow is not None:
                win = idx[pos + 1: pos + n_days + 1]
                vals = flow.reindex(win)
                if vals.notna().all():
                    after_f.append(float(vals.sum()))
                    rec["foreign_after"] = float(vals.sum())
        per_event.append(rec)

    return EventStudyResult(
        n_days=n_days, n_events=len(event_dates), n_excluded=excluded,
        before=_return_stats(before_r), after=_return_stats(after_r),
        flow_before=_flow_stats(before_f), flow_after=_flow_stats(after_f),
        per_event=per_event,
    )
