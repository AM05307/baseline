"""
scripts/compute_signals.py의 DART 연동 헬퍼(_dart_pbr_timeline·
_dart_operating_margin_timeline·_report_dart_vs_manual) 테스트.

나머지 compute_signals.py 로직(기존 신호들)은 이미 실제 데이터로 여러
세션 검증됐고 이 프로젝트 관행상 CLI는 얇은 배선이라 따로 테스트하지
않는다 — 이번에 추가한 DART 분기(실제 로직이 있는 부분)만 테스트한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from scripts import compute_signals as cs  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


class TestDartPbrTimeline:
    def test_empty_when_corp_code_unknown(self, tmp_db):
        idx = pd.bdate_range("2026-01-01", periods=5)
        close = pd.Series([100.0] * 5, index=idx)
        out = cs._dart_pbr_timeline("000660", idx, close)
        assert out.empty

    def test_computes_pbr_once_financials_present(self, tmp_db):
        store.upsert_disclosures([
            {"rcept_no": "1", "corp_code": "00164779", "stock_code": "000660", "rcept_dt": "2026-01-05"},
        ], path=tmp_db)
        store.upsert_financials([
            {"corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
             "account_nm": "자본총계", "fs_div": "CFS", "thstrm_amount": 1000.0, "rcept_dt": "2026-01-05"},
            {"corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
             "account_nm": "발행주식총수", "thstrm_amount": 100.0, "rcept_dt": "2026-01-05"},
        ], path=tmp_db)

        idx = pd.bdate_range("2026-01-01", periods=10)
        close = pd.Series([50.0] * 10, index=idx)  # BPS=10 -> PBR=5
        out = cs._dart_pbr_timeline("000660", idx, close)

        assert pd.isna(out.iloc[0])  # 1/5 공시 이전
        assert out.iloc[-1] == pytest.approx(5.0)

    def test_empty_when_equity_present_but_shares_missing(self, tmp_db):
        store.upsert_disclosures([
            {"rcept_no": "1", "corp_code": "00164779", "stock_code": "000660", "rcept_dt": "2026-01-05"},
        ], path=tmp_db)
        store.upsert_financials([
            {"corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
             "account_nm": "자본총계", "thstrm_amount": 1000.0, "rcept_dt": "2026-01-05"},
        ], path=tmp_db)
        idx = pd.bdate_range("2026-01-01", periods=5)
        close = pd.Series([50.0] * 5, index=idx)
        out = cs._dart_pbr_timeline("000660", idx, close)
        assert out.empty


class TestDartOperatingMarginTimeline:
    def test_empty_when_corp_code_unknown(self, tmp_db):
        assert cs._dart_operating_margin_timeline("000660").empty

    def test_derives_quarterly_margin_from_cumulative_reports(self, tmp_db):
        store.upsert_disclosures([
            {"rcept_no": "1", "corp_code": "00164779", "stock_code": "000660", "rcept_dt": "2026-05-15"},
        ], path=tmp_db)
        store.upsert_financials([
            {"corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
             "account_nm": "매출액", "thstrm_amount": 100.0, "rcept_dt": "2026-05-15"},
            {"corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
             "account_nm": "영업이익", "thstrm_amount": 20.0, "rcept_dt": "2026-05-15"},
        ], path=tmp_db)
        out = cs._dart_operating_margin_timeline("000660")
        assert len(out) == 1
        assert out.iloc[0] == pytest.approx(20.0)


class TestReportDartVsManual:
    def test_prints_comparison_when_dates_overlap(self, tmp_db, capsys):
        idx = pd.to_datetime(["2026-01-05"])
        dart = pd.Series([5.0], index=idx)
        manual = pd.Series([4.5], index=idx)
        cs._report_dart_vs_manual("TEST", dart, manual, verbose=True)
        out = capsys.readouterr().out
        assert "DART=5.00" in out and "수동입력=4.50" in out

    def test_silent_when_not_verbose(self, tmp_db, capsys):
        idx = pd.to_datetime(["2026-01-05"])
        cs._report_dart_vs_manual("TEST", pd.Series([5.0], index=idx), pd.Series([4.5], index=idx), verbose=False)
        assert capsys.readouterr().out == ""

    def test_silent_when_dart_empty(self, tmp_db, capsys):
        cs._report_dart_vs_manual("TEST", pd.Series(dtype=float), pd.Series([4.5], index=pd.to_datetime(["2026-01-05"])), verbose=True)
        assert capsys.readouterr().out == ""

    def test_never_mutates_manual_timeline(self, tmp_db):
        idx = pd.to_datetime(["2026-01-05"])
        dart = pd.Series([5.0], index=idx)
        manual = pd.Series([4.5], index=idx)
        manual_copy = manual.copy()
        cs._report_dart_vs_manual("TEST", dart, manual, verbose=True)
        pd.testing.assert_series_equal(manual, manual_copy)  # 비교만, 수정 없음
