"""
DART 원본 재무계정(db.store.get_financials()가 주는 DataFrame)을 신호
계산에 쓸 수 있는 "공시일 기준 as-of" 시계열로 바꾸는 순수 함수들.
analysis/ 관례대로 DB/API를 모른다 — collectors/dart_client.py가 받아온
데이터를 호출부(scripts/compute_signals.py)가 이 모듈 모양으로 바꿔 넘긴다.

**미래 참조 방지가 핵심**: DART 재무 데이터는 "사업연도"(그 숫자가 가리키는
기간)와 "공시일"(그 숫자가 실제로 세상에 공개된 날짜, rcept_dt)이 다르다
— 예를 들어 3분기 실적은 보통 그 분기가 끝난 지 한 달 넘게 지나서야
공시된다. 날짜 t의 PBR/영업이익률을 계산할 때 반드시 **rcept_dt<=t인
가장 최근 보고서**만 써야 한다(그 이후 보고서를 쓰면 미래참조). 이 모듈의
as-of 함수들은 전부 이 규칙을 pd.merge_asof(direction="backward")로
강제한다 — analysis/risk_score_history.py의 weights_as_of()와 같은 패턴.

**분기 단독값 역산**: 손익계정(매출액·영업이익 등)의 분기/반기/사업보고서
금액은 그 사업연도 1월 1일부터 해당 기간 말까지의 "누적" 금액이다. 당해
분기만의 값이 필요하면 바로 이전 보고서의 누적값을 빼야 한다:
    Q1 단독 = Q1 누적(그 자체)
    Q2 단독 = 반기 누적 - Q1 누적
    Q3 단독 = 3분기 누적 - 반기 누적
    Q4 단독 = 사업보고서(연간) 누적 - 3분기 누적
재무상태표 계정(자본총계 등)은 "그 시점 스냅샷"이라 역산이 필요 없다.
중간 보고서가 비어 있으면(예: 반기보고서 누락) 그 뒤 분기부터는 역산
체인이 끊겨서 계산하지 않는다(지어내지 않음).

(collectors/dart_client.py에도 REPORT_CODE_* 상수가 있다 — analysis/ 모듈이
collectors/를 import하지 않는 기존 관행을 지키기 위해 값을 의도적으로
중복해뒀다. 두 쪽이 같은 값을 쓰는지는 tests/test_dart_financials.py가
직접 비교해서 확인한다.)
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

REPORT_CODE_Q1 = "11013"
REPORT_CODE_HALF = "11012"
REPORT_CODE_Q3 = "11014"
REPORT_CODE_ANNUAL = "11011"
REPORT_CODE_CUMULATIVE_ORDER = [REPORT_CODE_Q1, REPORT_CODE_HALF, REPORT_CODE_Q3, REPORT_CODE_ANNUAL]

# "발행주식총수"는 정식 재무제표 계정이 아니지만, financials 테이블의 같은
# as-of 조회 패턴(_account_series)을 재사용하기 위해 합성 계정명으로 저장한다
# (collectors/dart_client.collect_shares_outstanding() 참고).
SHARES_OUTSTANDING_ACCOUNT_NM = "발행주식총수"


@dataclass
class QuarterlyFigure:
    bsns_year: str
    quarter: int  # 1~4
    revenue: float
    operating_income: float
    operating_margin_pct: float | None  # revenue가 0이면 None(0으로 나누지 않음)
    rcept_dt: str  # 이 분기 단독값을 역산할 수 있게 된(그 분기 보고서가 공시된) 날짜


def _account_series(financials_df: pd.DataFrame, account_nm: str, prefer_fs_div: str = "CFS") -> pd.DataFrame:
    """
    financials_df에서 account_nm 하나만 골라 rcept_dt 오름차순으로 정렬해
    돌려준다. 연결(CFS)이 있으면 우선 쓰고 없으면 별도(OFS)로 대체한다
    (연결이 더 널리 참조되는 지표라고 보고 — TODO: 공식 가이드 재확인).
    """
    if financials_df.empty:
        return financials_df
    sub = financials_df[financials_df["account_nm"] == account_nm]
    if sub.empty:
        return sub
    if "fs_div" in sub.columns and (sub["fs_div"] == prefer_fs_div).any():
        sub = sub[sub["fs_div"] == prefer_fs_div]
    return sub.sort_values("rcept_dt")


def _asof_value(series: pd.Series, target_index: pd.DatetimeIndex) -> pd.Series:
    """
    series(인덱스=공시일)를 target_index에 "그 날짜까지 공시된 가장 최근
    값"으로 맞춘다(merge_asof backward — 미래 참조 금지). series가 비어
    있으면 전부 NaN(지어내지 않음).
    """
    target_index = pd.DatetimeIndex(target_index)
    if series.empty or len(target_index) == 0:
        return pd.Series(float("nan"), index=target_index)
    s = series.sort_index()
    left = pd.DataFrame({"date": target_index}).sort_values("date")
    right = pd.DataFrame({"date": pd.DatetimeIndex(s.index), "value": s.to_numpy()}).sort_values("date")
    merged = pd.merge_asof(left, right, on="date", direction="backward")
    return pd.Series(merged["value"].to_numpy(), index=merged["date"]).reindex(target_index)


def equity_as_of_series(financials_df: pd.DataFrame) -> pd.Series:
    """'자본총계' 계정을 rcept_dt 인덱스의 Series(오름차순)로. 데이터가 없으면 빈 Series."""
    sub = _account_series(financials_df, "자본총계")
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.set_index("rcept_dt")["thstrm_amount"].sort_index()


def shares_outstanding_as_of_series(financials_df: pd.DataFrame) -> pd.Series:
    """SHARES_OUTSTANDING_ACCOUNT_NM(합성 계정) 시계열. 데이터가 없으면 빈 Series."""
    sub = _account_series(financials_df, SHARES_OUTSTANDING_ACCOUNT_NM)
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.set_index("rcept_dt")["thstrm_amount"].sort_index()


def bps_as_of(equity_series: pd.Series, shares_series: pd.Series, target_index: pd.DatetimeIndex) -> pd.Series:
    """
    target_index(보통 일별 종가 날짜)에 대해, 그 날짜까지 공시된 가장 최근
    자본총계·발행주식수로 BPS(주당순자산)를 계산한다. 둘 중 하나라도 그
    시점까지 공시된 적 없으면 NaN(지어내지 않음) — "발행주식수 확인 불가"
    (shares_series가 아예 비어 있는 경우)도 이 경로로 자연스럽게 NaN이 된다.
    """
    equity = _asof_value(equity_series, target_index)
    shares = _asof_value(shares_series, target_index)
    bps = equity / shares
    bps = bps.mask((shares == 0) | shares.isna())
    return bps


def pbr_series(close: pd.Series, bps: pd.Series) -> pd.Series:
    """PBR = 종가 / BPS. close·bps는 같은 인덱스로 미리 맞춰서 넘긴다(호출부 책임)."""
    pbr = close / bps
    return pbr.mask((bps == 0) | bps.isna())


def quarterly_operating_margin(financials_df: pd.DataFrame) -> list[QuarterlyFigure]:
    """
    '매출액'·'영업이익' 누적 계정에서 분기 단독값을 역산해 분기별
    영업이익률(%) 목록을 돌려준다(bsns_year·quarter 오름차순은 보장 안 함
    — 호출부가 필요하면 정렬). 역산 체인이 끊기면(중간 보고서 누락, 금액
    NaN 등) 그 사업연도의 그 뒤 분기부터는 계산하지 않는다.
    """
    revenue = _account_series(financials_df, "매출액")
    op_income = _account_series(financials_df, "영업이익")
    if revenue.empty or op_income.empty:
        return []

    results: list[QuarterlyFigure] = []
    years = sorted(set(revenue["bsns_year"]) | set(op_income["bsns_year"]))
    for year in years:
        rev_by_code = revenue[revenue["bsns_year"] == year].set_index("reprt_code")
        op_by_code = op_income[op_income["bsns_year"] == year].set_index("reprt_code")
        prev_rev_cum, prev_op_cum = 0.0, 0.0
        chain_ok = True
        for q_idx, reprt_code in enumerate(REPORT_CODE_CUMULATIVE_ORDER, start=1):
            present = reprt_code in rev_by_code.index and reprt_code in op_by_code.index
            if not present:
                chain_ok = False
                continue
            if not chain_ok:
                continue
            rev_cum = rev_by_code.loc[reprt_code, "thstrm_amount"]
            op_cum = op_by_code.loc[reprt_code, "thstrm_amount"]
            if pd.isna(rev_cum) or pd.isna(op_cum):
                chain_ok = False
                continue
            rcept_dt = rev_by_code.loc[reprt_code, "rcept_dt"]
            rev_q = float(rev_cum) - prev_rev_cum
            op_q = float(op_cum) - prev_op_cum
            margin = (op_q / rev_q * 100) if rev_q else None
            results.append(QuarterlyFigure(
                bsns_year=str(year), quarter=q_idx, revenue=rev_q, operating_income=op_q,
                operating_margin_pct=margin,
                rcept_dt=str(rcept_dt.date()) if hasattr(rcept_dt, "date") else str(rcept_dt),
            ))
            prev_rev_cum, prev_op_cum = float(rev_cum), float(op_cum)
    return results


def quarterly_operating_margin_timeline(financials_df: pd.DataFrame) -> pd.Series:
    """
    quarterly_operating_margin()의 결과를 rcept_dt 인덱스의 영업이익률(%)
    Series로(오름차순) 바꾼다 — analysis/signals.yjh_memory_margin()에 그대로
    넘길 수 있는 모양(기존 manual_inputs 기반 margin_timeline과 같은 모양).
    """
    figures = quarterly_operating_margin(financials_df)
    valid = [f for f in figures if f.operating_margin_pct is not None]
    if not valid:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex([f.rcept_dt for f in valid])
    return pd.Series([f.operating_margin_pct for f in valid], index=idx).sort_index()
