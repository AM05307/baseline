"""
신호값을 한국 결정일(KRX 거래일)에 맞춰 정렬하는 공통 함수.

signal_values 테이블은 신호마다 **원천 시세의 날짜**로 저장된다. 한국 장 신호는 KRX
날짜로, 미국 금리·미국 ETF·해외 지수로 계산하는 신호는 그 시장의 날짜로 들어간다.
그런데 해외 세션의 t일 종가는 한국 t일 장 마감(15:30 KST)보다 늦게 나온다 — 미국은
한국 시간 다음 날 새벽, 유럽·인도·브라질도 한국 장 마감 뒤에 끝난다. 그래서 같은
날짜끼리 그대로 맞추면(reindex) 한국 t일 종가 시점에 아직 모르는 값을 쓰는 반나절
미래참조가 된다.

규칙(2026-10-18 what_if 백테스트에서 발견, 이후 날짜별 위험점수·신호 평가 경로 전부에
적용):
  - PRIOR_SESSION_SIGNALS: 결정일 t에는 **t보다 엄격히 이전** 날짜의 값만 쓴다.
  - 나머지(한국 장 신호): t일 값까지 쓴다.
  - 원천 시장이 쉬어서 그 날 값이 없으면 **직전 값을 유지**한다. 예전 방식(같은 날짜로
    reindex 후 없는 날 0)은 미국 휴장일마다 신호를 "꺼짐"으로 바꿨다.
  - 단, max_gap_days(달력일)보다 오래된 값은 "데이터 없음"(NaN) — 수집이 끊긴 뒤 몇 달
    전 값을 끌어오지 않는다.

이 모듈은 analysis/ 관례대로 순수 함수만 담는다(DB/API 비의존).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 한국 장 마감 뒤에 끝나는 세션의 종가로 계산되는 신호(scripts/compute_signals.py 기준).
#   YJH_US10Y·KSJ_US10Y_HIGH: 미국 10년물(^TNX), 미국 날짜
#   KHJ_IPO_WEAK: 미국 IPO ETF·S&P500
#   KHJ_CONCENTRATION: 미국·유럽·인도·브라질·튀르키예·베트남(ETF) 지수
#   KHJ_COMBO: 위 신호들의 조합(날짜도 그 합집합)
# 새 신호를 추가할 때 원천이 해외 세션이면 여기에도 넣어야 한다.
PRIOR_SESSION_SIGNALS: frozenset[str] = frozenset(
    {"YJH_US10Y", "KSJ_US10Y_HIGH", "KHJ_IPO_WEAK", "KHJ_CONCENTRATION", "KHJ_COMBO"}
)

MAX_SIGNAL_GAP_DAYS = 5  # 미국 최장 연휴(4일)보다 길게, 수집 중단은 걸러질 만큼 짧게


def align_values(
    values: pd.DataFrame,
    decision_dates: pd.DatetimeIndex,
    prior_session: bool,
    max_gap_days: int = MAX_SIGNAL_GAP_DAYS,
) -> pd.DataFrame:
    """
    values: DataFrame(date, value, is_on) — db.store.get_signal_values() 모양.
    반환: DataFrame(index=decision_dates, columns=[value, is_on, src_date]). 결정일마다
    "그때 알 수 있던 가장 최근 값"과 그 값의 원래 날짜(src_date). 없으면 NaN/NaT.
    """
    # merge_asof는 양쪽 날짜 해상도(s·us·ns)가 다르면 실패한다(pandas 3) — 여기서 ns로 통일한다.
    decision_dates = pd.DatetimeIndex(decision_dates).as_unit("ns")
    out = pd.DataFrame(index=decision_dates, columns=["value", "is_on", "src_date"])
    out["value"] = np.nan
    out["is_on"] = np.nan
    out["src_date"] = pd.NaT
    if values is None or values.empty or len(decision_dates) == 0:
        return out
    right = values[["date", "value", "is_on"]].copy()
    right["date"] = pd.to_datetime(right["date"]).dt.as_unit("ns")
    right = right.drop_duplicates("date", keep="last").sort_values("date")
    right["src_date"] = right["date"]
    left = pd.DataFrame({"date": decision_dates.sort_values()})
    merged = pd.merge_asof(
        left, right, on="date", direction="backward",
        allow_exact_matches=not prior_session,
        tolerance=pd.Timedelta(days=max_gap_days),
    ).set_index("date")
    out["value"] = merged["value"].astype(float).reindex(decision_dates)
    out["is_on"] = merged["is_on"].astype(float).reindex(decision_dates)
    out["src_date"] = merged["src_date"].reindex(decision_dates)
    return out


def aligned_is_on(
    signal_id: str,
    values: pd.DataFrame,
    decision_dates: pd.DatetimeIndex,
    prior_session_signals: frozenset[str] | set[str] = PRIOR_SESSION_SIGNALS,
    max_gap_days: int = MAX_SIGNAL_GAP_DAYS,
) -> pd.Series:
    """신호 하나의 is_on을 결정일에 맞춘 Series(float, 값 없으면 NaN)."""
    return align_values(values, decision_dates, signal_id in prior_session_signals, max_gap_days)["is_on"]


def align_signals(
    values_by_signal: dict[str, pd.DataFrame],
    decision_dates: pd.DatetimeIndex,
    prior_session_signals: frozenset[str] | set[str] = PRIOR_SESSION_SIGNALS,
    max_gap_days: int = MAX_SIGNAL_GAP_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """여러 신호를 한꺼번에. 반환: (is_on, value), 둘 다 index=decision_dates, columns=signal_id."""
    decision_dates = pd.DatetimeIndex(decision_dates).as_unit("ns")
    is_on = pd.DataFrame(np.nan, index=decision_dates, columns=list(values_by_signal), dtype=float)
    value = is_on.copy()
    for sid, df in values_by_signal.items():
        a = align_values(df, decision_dates, sid in prior_session_signals, max_gap_days)
        is_on[sid] = a["is_on"]
        value[sid] = a["value"]
    return is_on, value
