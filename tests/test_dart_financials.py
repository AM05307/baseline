"""analysis/dart_financials.py 테스트 — 합성 데이터만 사용(DB 비의존)."""

from __future__ import annotations

import pandas as pd
import pytest

from analysis import dart_financials as df_mod
from collectors import dart_client as dc


def _financials_df(rows: list[dict]) -> pd.DataFrame:
    out = pd.DataFrame(rows)
    out["rcept_dt"] = pd.to_datetime(out["rcept_dt"])
    return out


class TestReportCodeConstantsMatchCollectorsModule:
    def test_same_values_as_dart_client(self):
        assert df_mod.REPORT_CODE_CUMULATIVE_ORDER == dc.REPORT_CODE_CUMULATIVE_ORDER
        assert df_mod.REPORT_CODE_Q1 == dc.REPORT_CODE_Q1
        assert df_mod.REPORT_CODE_ANNUAL == dc.REPORT_CODE_ANNUAL


class TestEquityAsOfSeries:
    def test_extracts_equity_sorted_by_date(self):
        rows = [
            {"account_nm": "자본총계", "fs_div": "CFS", "thstrm_amount": 1000.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "자본총계", "fs_div": "CFS", "thstrm_amount": 1100.0, "rcept_dt": "2026-08-15"},
            {"account_nm": "매출액", "fs_div": "CFS", "thstrm_amount": 500.0, "rcept_dt": "2026-05-15"},
        ]
        s = df_mod.equity_as_of_series(_financials_df(rows))
        assert len(s) == 2
        assert list(s.values) == [1000.0, 1100.0]

    def test_empty_when_no_equity_account(self):
        rows = [{"account_nm": "매출액", "fs_div": "CFS", "thstrm_amount": 500.0, "rcept_dt": "2026-05-15"}]
        assert df_mod.equity_as_of_series(_financials_df(rows)).empty

    def test_prefers_cfs_over_ofs(self):
        rows = [
            {"account_nm": "자본총계", "fs_div": "OFS", "thstrm_amount": 900.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "자본총계", "fs_div": "CFS", "thstrm_amount": 1000.0, "rcept_dt": "2026-05-15"},
        ]
        s = df_mod.equity_as_of_series(_financials_df(rows))
        assert list(s.values) == [1000.0]

    def test_falls_back_to_ofs_when_cfs_absent(self):
        rows = [{"account_nm": "자본총계", "fs_div": "OFS", "thstrm_amount": 900.0, "rcept_dt": "2026-05-15"}]
        s = df_mod.equity_as_of_series(_financials_df(rows))
        assert list(s.values) == [900.0]

    def test_empty_input_dataframe(self):
        assert df_mod.equity_as_of_series(pd.DataFrame()).empty


class TestBpsAsOf:
    def test_no_lookahead_uses_most_recent_prior_disclosure(self):
        equity = pd.Series([1000.0, 1200.0], index=pd.to_datetime(["2026-01-10", "2026-07-10"]))
        shares = pd.Series([100.0], index=pd.to_datetime(["2026-01-10"]))
        target = pd.date_range("2026-01-01", periods=10, freq="MS")  # 월별
        bps = df_mod.bps_as_of(equity, shares, target)
        # 2026-01-01은 1/10 공시 이전 -> NaN
        assert pd.isna(bps.loc["2026-01-01"])
        # 2026-02-01 이후는 1000/100=10 (7월 이후 equity가 바뀌지만 shares 불변)
        assert bps.loc["2026-02-01"] == pytest.approx(10.0)
        assert bps.loc["2026-08-01"] == pytest.approx(12.0)

    def test_empty_shares_series_gives_all_nan(self):
        equity = pd.Series([1000.0], index=pd.to_datetime(["2026-01-10"]))
        shares = pd.Series(dtype=float)
        target = pd.date_range("2026-02-01", periods=3)
        bps = df_mod.bps_as_of(equity, shares, target)
        assert bps.isna().all()

    def test_zero_shares_does_not_divide_by_zero(self):
        equity = pd.Series([1000.0], index=pd.to_datetime(["2026-01-10"]))
        shares = pd.Series([0.0], index=pd.to_datetime(["2026-01-10"]))
        target = pd.date_range("2026-02-01", periods=1)
        bps = df_mod.bps_as_of(equity, shares, target)
        assert bps.isna().all()  # 0으로 나누지 않고 NaN


class TestPbrSeries:
    def test_basic_division(self):
        idx = pd.date_range("2026-01-01", periods=2)
        close = pd.Series([100.0, 120.0], index=idx)
        bps = pd.Series([10.0, 10.0], index=idx)
        pbr = df_mod.pbr_series(close, bps)
        assert list(pbr.values) == [10.0, 12.0]

    def test_nan_bps_propagates_to_nan_pbr(self):
        idx = pd.date_range("2026-01-01", periods=1)
        close = pd.Series([100.0], index=idx)
        bps = pd.Series([float("nan")], index=idx)
        assert df_mod.pbr_series(close, bps).isna().all()


class TestQuarterlyOperatingMargin:
    def test_reconstructs_each_quarter_from_cumulative(self):
        rows = [
            {"account_nm": "매출액", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 100.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "영업이익", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 20.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "매출액", "reprt_code": "11012", "bsns_year": "2026", "thstrm_amount": 210.0, "rcept_dt": "2026-08-14"},
            {"account_nm": "영업이익", "reprt_code": "11012", "bsns_year": "2026", "thstrm_amount": 50.0, "rcept_dt": "2026-08-14"},
        ]
        figures = df_mod.quarterly_operating_margin(_financials_df(rows))
        assert len(figures) == 2
        q1, q2 = figures
        assert q1.quarter == 1 and q1.revenue == pytest.approx(100.0) and q1.operating_income == pytest.approx(20.0)
        assert q1.operating_margin_pct == pytest.approx(20.0)
        # Q2 단독 = 210-100=110 매출, 50-20=30 영업이익 -> 27.27%
        assert q2.quarter == 2 and q2.revenue == pytest.approx(110.0) and q2.operating_income == pytest.approx(30.0)
        assert q2.operating_margin_pct == pytest.approx(30 / 110 * 100)
        assert q2.rcept_dt == "2026-08-14"

    def test_missing_middle_quarter_breaks_chain(self):
        # Q1만 있고 반기 없음, 3분기만 있음 -> Q1만 계산되고 Q3는 역산 불가(건너뜀)
        rows = [
            {"account_nm": "매출액", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 100.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "영업이익", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 20.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "매출액", "reprt_code": "11014", "bsns_year": "2026", "thstrm_amount": 330.0, "rcept_dt": "2026-11-14"},
            {"account_nm": "영업이익", "reprt_code": "11014", "bsns_year": "2026", "thstrm_amount": 70.0, "rcept_dt": "2026-11-14"},
        ]
        figures = df_mod.quarterly_operating_margin(_financials_df(rows))
        assert len(figures) == 1
        assert figures[0].quarter == 1

    def test_zero_revenue_quarter_gives_none_margin_not_crash(self):
        rows = [
            {"account_nm": "매출액", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 0.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "영업이익", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": -5.0, "rcept_dt": "2026-05-15"},
        ]
        figures = df_mod.quarterly_operating_margin(_financials_df(rows))
        assert figures[0].operating_margin_pct is None

    def test_empty_when_no_revenue_or_op_income(self):
        rows = [{"account_nm": "자본총계", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 1000.0, "rcept_dt": "2026-05-15"}]
        assert df_mod.quarterly_operating_margin(_financials_df(rows)) == []

    def test_multiple_years_independent(self):
        rows = [
            {"account_nm": "매출액", "reprt_code": "11013", "bsns_year": "2025", "thstrm_amount": 90.0, "rcept_dt": "2025-05-15"},
            {"account_nm": "영업이익", "reprt_code": "11013", "bsns_year": "2025", "thstrm_amount": 10.0, "rcept_dt": "2025-05-15"},
            {"account_nm": "매출액", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 100.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "영업이익", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 20.0, "rcept_dt": "2026-05-15"},
        ]
        figures = df_mod.quarterly_operating_margin(_financials_df(rows))
        assert len(figures) == 2
        assert {f.bsns_year for f in figures} == {"2025", "2026"}


class TestQuarterlyOperatingMarginTimeline:
    def test_produces_series_indexed_by_rcept_dt(self):
        rows = [
            {"account_nm": "매출액", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 100.0, "rcept_dt": "2026-05-15"},
            {"account_nm": "영업이익", "reprt_code": "11013", "bsns_year": "2026", "thstrm_amount": 20.0, "rcept_dt": "2026-05-15"},
        ]
        s = df_mod.quarterly_operating_margin_timeline(_financials_df(rows))
        assert len(s) == 1
        assert s.iloc[0] == pytest.approx(20.0)

    def test_empty_when_no_valid_margins(self):
        assert df_mod.quarterly_operating_margin_timeline(pd.DataFrame()).empty
