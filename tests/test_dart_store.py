"""db/store.py의 financials/disclosures 테이블 함수 테스트(DART 연동 1부)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


class TestUpsertFinancials:
    def test_idempotent_rerun_does_not_duplicate(self, tmp_db):
        row = {
            "corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
            "account_nm": "자본총계", "fs_div": "CFS", "thstrm_amount": 1000.0,
            "rcept_no": "20260101000001", "rcept_dt": "2026-05-15",
        }
        store.upsert_financials([row], path=tmp_db)
        store.upsert_financials([row], path=tmp_db)
        df = store.get_financials("00164779", path=tmp_db)
        assert len(df) == 1

    def test_rerun_with_updated_amount_overwrites(self, tmp_db):
        base = {
            "corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
            "account_nm": "자본총계", "rcept_dt": "2026-05-15",
        }
        store.upsert_financials([{**base, "thstrm_amount": 1000.0}], path=tmp_db)
        store.upsert_financials([{**base, "thstrm_amount": 1200.0}], path=tmp_db)
        df = store.get_financials("00164779", "자본총계", path=tmp_db)
        assert len(df) == 1
        assert df.iloc[0]["thstrm_amount"] == 1200.0

    def test_different_accounts_do_not_collide(self, tmp_db):
        base = {"corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013", "rcept_dt": "2026-05-15"}
        store.upsert_financials([
            {**base, "account_nm": "자본총계", "thstrm_amount": 1000.0},
            {**base, "account_nm": "매출액", "thstrm_amount": 500.0},
        ], path=tmp_db)
        df = store.get_financials("00164779", path=tmp_db)
        assert len(df) == 2

    def test_empty_rows_returns_zero(self, tmp_db):
        assert store.upsert_financials([], path=tmp_db) == 0


class TestFinancialReportExists:
    def test_false_when_not_fetched(self, tmp_db):
        assert store.financial_report_exists("00164779", "2026", "11013", path=tmp_db) is False

    def test_true_after_any_account_stored(self, tmp_db):
        store.upsert_financials([{
            "corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
            "account_nm": "자본총계", "rcept_dt": "2026-05-15",
        }], path=tmp_db)
        assert store.financial_report_exists("00164779", "2026", "11013", path=tmp_db) is True

    def test_different_report_code_not_confused(self, tmp_db):
        store.upsert_financials([{
            "corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
            "account_nm": "자본총계", "rcept_dt": "2026-05-15",
        }], path=tmp_db)
        assert store.financial_report_exists("00164779", "2026", "11012", path=tmp_db) is False


class TestGetFinancials:
    def test_filters_by_account_nm(self, tmp_db):
        base = {"corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013", "rcept_dt": "2026-05-15"}
        store.upsert_financials([
            {**base, "account_nm": "자본총계", "thstrm_amount": 1000.0},
            {**base, "account_nm": "매출액", "thstrm_amount": 500.0},
        ], path=tmp_db)
        df = store.get_financials("00164779", "매출액", path=tmp_db)
        assert len(df) == 1 and df.iloc[0]["thstrm_amount"] == 500.0

    def test_empty_when_no_data(self, tmp_db):
        assert store.get_financials("00000000", path=tmp_db).empty


class TestUpsertDisclosures:
    def test_idempotent_rerun_does_not_duplicate(self, tmp_db):
        row = {"rcept_no": "20260101000001", "corp_code": "00164779", "stock_code": "000660",
               "report_nm": "분기보고서", "rcept_dt": "2026-05-15"}
        store.upsert_disclosures([row], path=tmp_db)
        store.upsert_disclosures([row], path=tmp_db)
        df = store.get_disclosures("000660", path=tmp_db)
        assert len(df) == 1

    def test_update_refreshes_fields(self, tmp_db):
        base = {"rcept_no": "20260101000001", "stock_code": "000660", "rcept_dt": "2026-05-15"}
        store.upsert_disclosures([{**base, "report_nm": "분기보고서"}], path=tmp_db)
        store.upsert_disclosures([{**base, "report_nm": "[기재정정]분기보고서"}], path=tmp_db)
        df = store.get_disclosures("000660", path=tmp_db)
        assert df.iloc[0]["report_nm"] == "[기재정정]분기보고서"

    def test_empty_rows_returns_zero(self, tmp_db):
        assert store.upsert_disclosures([], path=tmp_db) == 0


class TestDisclosureExists:
    def test_false_then_true(self, tmp_db):
        assert store.disclosure_exists("20260101000001", path=tmp_db) is False
        store.upsert_disclosures([{"rcept_no": "20260101000001", "rcept_dt": "2026-05-15"}], path=tmp_db)
        assert store.disclosure_exists("20260101000001", path=tmp_db) is True


class TestGetCorpCodeForStock:
    def test_none_when_nothing_collected(self, tmp_db):
        assert store.get_corp_code_for_stock("000660", path=tmp_db) is None

    def test_found_via_disclosures(self, tmp_db):
        store.upsert_disclosures([
            {"rcept_no": "1", "corp_code": "00164779", "stock_code": "000660", "rcept_dt": "2026-01-01"},
        ], path=tmp_db)
        assert store.get_corp_code_for_stock("000660", path=tmp_db) == "00164779"

    def test_falls_back_to_financials_when_no_disclosures(self, tmp_db):
        store.upsert_financials([
            {"corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
             "account_nm": "자본총계", "stock_code": "000660", "rcept_dt": "2026-05-15"},
        ], path=tmp_db)
        assert store.get_corp_code_for_stock("000660", path=tmp_db) == "00164779"

    def test_disclosures_preferred_over_financials(self, tmp_db):
        store.upsert_financials([
            {"corp_code": "WRONG", "bsns_year": "2026", "reprt_code": "11013",
             "account_nm": "자본총계", "stock_code": "000660", "rcept_dt": "2026-05-15"},
        ], path=tmp_db)
        store.upsert_disclosures([
            {"rcept_no": "1", "corp_code": "00164779", "stock_code": "000660", "rcept_dt": "2026-01-01"},
        ], path=tmp_db)
        assert store.get_corp_code_for_stock("000660", path=tmp_db) == "00164779"


class TestGetDisclosures:
    def test_sorted_descending_by_date(self, tmp_db):
        store.upsert_disclosures([
            {"rcept_no": "1", "stock_code": "000660", "rcept_dt": "2026-01-01"},
            {"rcept_no": "2", "stock_code": "000660", "rcept_dt": "2026-06-01"},
        ], path=tmp_db)
        df = store.get_disclosures("000660", path=tmp_db)
        assert list(df["rcept_no"]) == ["2", "1"]

    def test_no_filter_returns_all(self, tmp_db):
        store.upsert_disclosures([
            {"rcept_no": "1", "stock_code": "000660", "rcept_dt": "2026-01-01"},
            {"rcept_no": "2", "stock_code": "005930", "rcept_dt": "2026-01-01"},
        ], path=tmp_db)
        assert len(store.get_disclosures(path=tmp_db)) == 2
