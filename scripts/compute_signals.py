"""
analysis/signals.py의 각 함수를 실제 수집된 데이터로 돌려서 signal_values
테이블을 채우는 CLI. "정의"(signals 테이블, scripts/seed_signals.py)와
"계산"(이 스크립트)을 분리해뒀다 — 신호 설명 문구를 고치는 것과 신호 값을
다시 계산하는 건 서로 다른 일이기 때문이다.

멱등이다(원칙 6) — db.store.upsert_signal_values가 (date, signal_id)로
덮어쓴다. 몇 번을 다시 돌려도 안전하다.

필요한 원본 데이터가 없는 신호는 조용히 건너뛰고 요약에 "skip" 사유를
남긴다(원칙: 없는 데이터로 지어내지 않는다) — 예를 들어 KHJ_RATE_STAGE는
scripts/add_manual.py로 금리 단계를 한 번도 입력하지 않았으면 전부 NaN이라
저장할 is_on이 없다.

사용:
    python scripts/compute_signals.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import dart_financials as dartfin  # noqa: E402
from analysis import signals as sig  # noqa: E402
from db import store  # noqa: E402

HYNIX = "000660"
MARKET_TICKERS = ["KOSDAQ", "SPX", "DJI", "STOXX50", "SENSEX", "BOVESPA", "VNINDEX", "BIST100", "SILVER"]


def _dart_pbr_timeline(stock_code: str, target_index: pd.DatetimeIndex, close: pd.Series) -> pd.Series:
    """
    DART 재무(자본총계·발행주식수)로 target_index(일별)에 대한 PBR 시계열을
    만든다. corp_code를 모르거나(DART 데이터 자체가 없음) 자본총계/발행
    주식수 중 하나가 없으면 빈 Series(지어내지 않음, 호출부가 수동입력으로
    대체한다).
    """
    corp_code = store.get_corp_code_for_stock(stock_code)
    if corp_code is None:
        return pd.Series(dtype=float)
    financials_df = store.get_financials(corp_code)
    if financials_df.empty:
        return pd.Series(dtype=float)
    equity = dartfin.equity_as_of_series(financials_df)
    shares = dartfin.shares_outstanding_as_of_series(financials_df)
    if equity.empty or shares.empty:
        return pd.Series(dtype=float)
    bps = dartfin.bps_as_of(equity, shares, target_index)
    return dartfin.pbr_series(close.reindex(target_index), bps)


def _dart_operating_margin_timeline(stock_code: str) -> pd.Series:
    """DART 손익계정에서 역산한 000660 자체 분기 영업이익률(%) 시계열
    (rcept_dt 인덱스). corp_code/재무 데이터가 없으면 빈 Series."""
    corp_code = store.get_corp_code_for_stock(stock_code)
    if corp_code is None:
        return pd.Series(dtype=float)
    financials_df = store.get_financials(corp_code)
    if financials_df.empty:
        return pd.Series(dtype=float)
    return dartfin.quarterly_operating_margin_timeline(financials_df)


def _report_dart_vs_manual(label: str, dart_timeline: pd.Series, manual_timeline: pd.Series, verbose: bool) -> None:
    """
    DART 값과 기존 manual_inputs 값을 **비교 리포트만** 출력한다 — 어느
    쪽도 자동으로 덮어쓰지 않는다(요청 그대로). manual_timeline은
    _manual_series()가 target_index에 forward-fill한 결과라 날짜가 많으니,
    DART 쪽 날짜(실제 공시일)에서만 비교한다.
    """
    if not verbose or dart_timeline.empty or not manual_timeline.notna().any():
        return
    overlap_idx = dart_timeline.index.intersection(manual_timeline.dropna().index)
    if len(overlap_idx) == 0:
        print(f"  (참고) {label}: DART 값과 수동입력 값이 겹치는 날짜가 없어 비교 못함")
        return
    for dt in overlap_idx:
        d_val, m_val = dart_timeline.loc[dt], manual_timeline.loc[dt]
        if d_val != d_val or m_val != m_val:
            continue
        diff = d_val - m_val
        print(f"  (비교) {label} {dt.date() if hasattr(dt, 'date') else dt}: DART={d_val:.2f} vs 수동입력={m_val:.2f} (차이 {diff:+.2f})")


def _manual_series(input_type: str, target_index: pd.DatetimeIndex, numeric: bool) -> pd.Series:
    """manual_inputs 타임라인을 target_index에 forward-fill. 입력이 없으면 전부 NaN."""
    timeline = store.get_manual_timeline(input_type)
    col = "value_num" if numeric else "value_text"
    if timeline.empty:
        return pd.Series(index=target_index, dtype=object)
    s = timeline.set_index("effective_on")[col].sort_index()
    return s.reindex(s.index.union(target_index)).ffill().reindex(target_index)


def _value_to_real(value) -> float | None:
    """signal_values.value는 REAL 컬럼이다 — KHJ_RATE_STAGE처럼 value가 문자열
    (단계 이름)인 신호는 is_on만 저장하고 value는 비워둔다."""
    if value is None or isinstance(value, str):
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    return float(value)


def _rows_from_df(df: pd.DataFrame, signal_id: str, is_on_col: str = "is_on") -> list[dict]:
    rows = []
    for dt, row in df.iterrows():
        is_on = row.get(is_on_col)
        if is_on is None or is_on != is_on:  # NaN/None -> 계산 불가, 저장 안 함
            continue
        rows.append({
            "date": dt.strftime("%Y-%m-%d"), "signal_id": signal_id,
            "value": _value_to_real(row.get("value")),
            "is_on": int(is_on),
        })
    return rows


def compute_all(verbose: bool = True) -> dict[str, int | str]:
    store.init_db()
    results: dict[str, int | str] = {}

    def save(signal_id: str, rows: list[dict]) -> None:
        n = store.upsert_signal_values(rows)
        results[signal_id] = n
        if verbose:
            print(f"  {signal_id:24s} {n:6d}행")

    def skip(signal_id: str, reason: str) -> None:
        results[signal_id] = f"skip: {reason}"
        if verbose:
            print(f"  {signal_id:24s} 건너뜀 ({reason})")

    close_hynix = store.get_prices(HYNIX)
    volume_hynix = store.get_volume(HYNIX)
    close_kospi = store.get_prices("KOSPI")
    close_samsung = store.get_prices("005930")
    close_ipo = store.get_prices("IPOETF")
    close_us10y = store.get_prices("US10Y")
    markets = {name: s for name in MARKET_TICKERS if not (s := store.get_prices(name)).empty}

    if close_hynix.empty:
        skip("KHJ_3DAY_SELLOFF", "000660 시세 없음")
        skip("KHJ_OVERHEAT", "000660 시세 없음")
        skip("YJH_BOX_RANGE_BUY", "000660 시세 없음")
        skip("YJH_BOX_RANGE_TRIM", "000660 시세 없음")
    else:
        save("KHJ_3DAY_SELLOFF", _rows_from_df(sig.khj_3day_selloff(close_hynix), "KHJ_3DAY_SELLOFF"))
        if volume_hynix.empty:
            skip("KHJ_OVERHEAT", "000660 거래량 없음")
        else:
            save("KHJ_OVERHEAT", _rows_from_df(sig.khj_overheat(close_hynix, volume_hynix), "KHJ_OVERHEAT"))
        box = sig.yjh_box_range(close_hynix)
        save("YJH_BOX_RANGE_BUY", _rows_from_df(box, "YJH_BOX_RANGE_BUY", "is_on_buy"))
        save("YJH_BOX_RANGE_TRIM", _rows_from_df(box, "YJH_BOX_RANGE_TRIM", "is_on_trim"))

    if len(markets) < len(MARKET_TICKERS):
        missing = set(MARKET_TICKERS) - set(markets)
        if verbose:
            print(f"  (참고) KHJ_CONCENTRATION: {len(markets)}/{len(MARKET_TICKERS)}개 시장만 있음, 없는 시장: {sorted(missing)}")
    if markets:
        save("KHJ_CONCENTRATION", _rows_from_df(sig.khj_concentration(markets), "KHJ_CONCENTRATION"))
    else:
        skip("KHJ_CONCENTRATION", "주변부 시장 데이터 없음 — collectors/global_markets.py 실행 필요")

    rate_target_index = close_hynix.index if not close_hynix.empty else pd.DatetimeIndex([])
    rate_timeline = _manual_series("rate_stage", rate_target_index, numeric=False)
    if rate_timeline.notna().any():
        save("KHJ_RATE_STAGE", _rows_from_df(sig.khj_rate_stage(rate_timeline), "KHJ_RATE_STAGE"))
    else:
        skip("KHJ_RATE_STAGE", "수동 입력 없음 — scripts/add_manual.py rate-stage 실행 필요")

    close_spx = markets.get("SPX", pd.Series(dtype=float))
    if close_ipo.empty or close_spx.empty:
        skip("KHJ_IPO_WEAK", "IPO ETF 또는 S&P500 데이터 없음")
    else:
        save("KHJ_IPO_WEAK", _rows_from_df(sig.khj_ipo_weak(close_ipo, close_spx), "KHJ_IPO_WEAK"))

    # KHJ_COMBO: 위 세 신호의 is_on을 DB에서 다시 읽어와 조합(막 계산한 값 재사용)
    def _saved_is_on(signal_id: str) -> pd.Series:
        """방금 save()한(또는 과거에 계산해둔) is_on 시계열. 비어 있으면 빈 Series."""
        df = store.get_signal_values(signal_id)
        return df.set_index("date")["is_on"] if not df.empty else pd.Series(dtype=float)

    rate_on, conc_on, ipo_on = _saved_is_on("KHJ_RATE_STAGE"), _saved_is_on("KHJ_CONCENTRATION"), _saved_is_on("KHJ_IPO_WEAK")
    if len(rate_on) or len(conc_on) or len(ipo_on):
        combo_index = rate_on.index.union(conc_on.index).union(ipo_on.index)
        save("KHJ_COMBO", _rows_from_df(
            sig.khj_combo(rate_on.reindex(combo_index), conc_on.reindex(combo_index), ipo_on.reindex(combo_index)),
            "KHJ_COMBO",
        ))
    else:
        skip("KHJ_COMBO", "하위 신호(RATE_STAGE/CONCENTRATION/IPO_WEAK) 전부 없음")

    if close_us10y.empty:
        skip("YJH_US10Y", "US10Y(^TNX) 데이터 없음")
        skip("KSJ_US10Y_HIGH", "US10Y(^TNX) 데이터 없음")
    else:
        save("YJH_US10Y", _rows_from_df(sig.yjh_us10y(close_us10y), "YJH_US10Y"))
        save("KSJ_US10Y_HIGH", _rows_from_df(sig.ksj_us10y_high(close_us10y), "KSJ_US10Y_HIGH"))

    # YJH_MEMORY_MARGIN: DART 연동 전까지는 "마이크론 분기 매출총이익률"
    # (수동입력)을 썼다. DART 연동 후에는 "000660 자체 분기 영업이익률"로
    # 계산 방식이 바뀐다(사용자 지정, 2026-10-07 세션) — DART 데이터가 있으면
    # 그걸 우선 쓰고, 없으면(키 미설정 등) 기존 수동입력으로 그대로 폴백한다
    # (하위호환 — DART 키 없이도 지금까지처럼 동작). 둘 다 있으면 자동으로
    # 덮어쓰지 않고 비교만 출력한다(요청 그대로).
    dart_margin_timeline = _dart_operating_margin_timeline(HYNIX)
    manual_margin_timeline = _manual_series("memory_margin", close_hynix.index if not close_hynix.empty else pd.DatetimeIndex([]), numeric=True)
    _report_dart_vs_manual("YJH_MEMORY_MARGIN(영업이익률 %)", dart_margin_timeline, manual_margin_timeline, verbose)
    if not dart_margin_timeline.empty:
        save("YJH_MEMORY_MARGIN", _rows_from_df(sig.yjh_memory_margin(dart_margin_timeline), "YJH_MEMORY_MARGIN"))
    elif manual_margin_timeline.notna().any():
        save("YJH_MEMORY_MARGIN", _rows_from_df(sig.yjh_memory_margin(manual_margin_timeline), "YJH_MEMORY_MARGIN"))
    else:
        skip("YJH_MEMORY_MARGIN", "DART 데이터 없음(DART_API_KEY 미설정 등) 및 수동 입력 없음 — "
                                   "scripts/add_manual.py memory-margin 실행 필요")

    # YJH_PBR_FLOOR: DART(자본총계·발행주식수)로 계산한 PBR을 우선 쓰고,
    # 없으면 기존 수동입력 PBR로 폴백(하위호환). 비교만, 자동 덮어쓰기 없음.
    dart_pbr_timeline = _dart_pbr_timeline(HYNIX, close_hynix.index, close_hynix) if not close_hynix.empty else pd.Series(dtype=float)
    manual_pbr_timeline = _manual_series("pbr", close_hynix.index if not close_hynix.empty else pd.DatetimeIndex([]), numeric=True)
    _report_dart_vs_manual("YJH_PBR_FLOOR(PBR)", dart_pbr_timeline.dropna(), manual_pbr_timeline, verbose)
    if dart_pbr_timeline.notna().any():
        save("YJH_PBR_FLOOR", _rows_from_df(sig.yjh_pbr_floor(dart_pbr_timeline), "YJH_PBR_FLOOR"))
    elif manual_pbr_timeline.notna().any():
        save("YJH_PBR_FLOOR", _rows_from_df(sig.yjh_pbr_floor(manual_pbr_timeline), "YJH_PBR_FLOOR"))
    else:
        skip("YJH_PBR_FLOOR", "DART 데이터 없음(DART_API_KEY 미설정 등) 및 수동 입력 없음 — "
                               "scripts/add_manual.py pbr 실행 필요")

    if close_kospi.empty:
        skip("KSJ_PANIC_DAY", "KOSPI 데이터 없음")
        skip("KSJ_BREADTH", "KOSPI 데이터 없음")
    else:
        save("KSJ_PANIC_DAY", _rows_from_df(sig.ksj_panic_day(close_kospi), "KSJ_PANIC_DAY"))
        if close_samsung.empty:
            skip("KSJ_BREADTH", "005930 데이터 없음")
        else:
            save("KSJ_BREADTH", _rows_from_df(sig.ksj_breadth(close_kospi, close_samsung, close_hynix), "KSJ_BREADTH"))

    foreigner_net = store.get_foreigner_net(HYNIX)
    if foreigner_net.empty:
        skip("KSJ_FOREIGN_SELL", "investor_flow 데이터 없음 — KIS investor_flow() 수집 필요(KIS 키 필요)")
    else:
        save("KSJ_FOREIGN_SELL", _rows_from_df(sig.ksj_foreign_sell(foreigner_net), "KSJ_FOREIGN_SELL"))

    skip("KSJ_DCA", "on/off 신호가 아니라 표시 포맷 규칙")

    return results


if __name__ == "__main__":
    print("[compute_signals] 신호 계산 시작")
    r = compute_all()
    computed = sum(1 for v in r.values() if isinstance(v, int))
    skipped = sum(1 for v in r.values() if isinstance(v, str))
    print(f"\n완료: {computed}개 신호 계산, {skipped}개 건너뜀")
