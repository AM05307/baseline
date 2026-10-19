"""scripts/seed_disclosure_claims.py 테스트. 네트워크 호출 없음 — disclosures
테이블이 이미 채워져 있다고 가정하고 그걸 claims로 옮기는 로직만 검증."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from scripts import seed_disclosure_claims as sdc  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _seed_disclosure(tmp_db, rcept_no="20260515000123", stock_code="000660", rcept_dt="2026-05-15",
                      report_nm="분기보고서 (2026.03)", flr_nm="SK하이닉스", corp_name="SK하이닉스"):
    store.upsert_disclosures([{
        "rcept_no": rcept_no, "corp_code": "00164779", "corp_name": corp_name,
        "stock_code": stock_code, "report_nm": report_nm, "rcept_dt": rcept_dt, "flr_nm": flr_nm,
    }], path=tmp_db)


class TestBuildClaimRow:
    def test_builds_official_confidence_claim_with_dart_url(self, tmp_db):
        _seed_disclosure(tmp_db)
        df = store.get_disclosures("000660", path=tmp_db)
        row = sdc.build_claim_row(df.iloc[0].to_dict())
        assert row["confidence"] == "official"
        assert row["source_url"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260515000123"
        assert "분기보고서" in row["summary"]
        assert row["ticker"] == "000660"
        assert row["stance"] is None

    def test_price_then_uses_price_as_of_disclosure_date_not_latest(self, tmp_db):
        store.upsert_prices([
            {"ticker": "000660", "date": "2026-05-10", "open": 1, "high": 1, "low": 1, "close": 100.0, "volume": 1, "source": "test"},
            {"ticker": "000660", "date": "2026-05-20", "open": 1, "high": 1, "low": 1, "close": 200.0, "volume": 1, "source": "test"},
        ], path=tmp_db)
        _seed_disclosure(tmp_db, rcept_dt="2026-05-15")
        df = store.get_disclosures("000660", path=tmp_db)
        row = sdc.build_claim_row(df.iloc[0].to_dict())
        assert row["price_then"] == 100.0  # 5/15 시점엔 5/10 종가가 최신(5/20은 미래)

    def test_price_then_none_when_no_price_data(self, tmp_db):
        _seed_disclosure(tmp_db)
        df = store.get_disclosures("000660", path=tmp_db)
        row = sdc.build_claim_row(df.iloc[0].to_dict())
        assert row["price_then"] is None


class TestSeedFromDisclosures:
    def test_seeds_new_disclosures(self, tmp_db):
        _seed_disclosure(tmp_db)
        result = sdc.seed_from_disclosures()
        assert result == {"seeded": 1, "skipped_existing": 0}
        df = store.get_latest_claims_by_person("000660", path=tmp_db)
        assert len(df) == 1
        assert df.iloc[0]["confidence"] == "official"

    def test_idempotent_rerun_skips_already_seeded(self, tmp_db):
        _seed_disclosure(tmp_db)
        sdc.seed_from_disclosures()
        result = sdc.seed_from_disclosures()  # 다시 실행
        assert result == {"seeded": 0, "skipped_existing": 1}
        # claims 테이블에 중복 행이 없는지 직접 확인
        with store.conn(tmp_db) as c:
            n = c.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"]
        assert n == 1

    def test_dry_run_does_not_write(self, tmp_db):
        _seed_disclosure(tmp_db)
        result = sdc.seed_from_disclosures(dry_run=True)
        assert result == {"seeded": 1, "skipped_existing": 0}
        with store.conn(tmp_db) as c:
            n = c.execute("SELECT COUNT(*) AS n FROM claims").fetchone()["n"]
        assert n == 0

    def test_filters_by_stock_code(self, tmp_db):
        _seed_disclosure(tmp_db, rcept_no="1", stock_code="000660")
        _seed_disclosure(tmp_db, rcept_no="2", stock_code="005930")
        result = sdc.seed_from_disclosures(stock_code="000660")
        assert result["seeded"] == 1

    def test_empty_disclosures_table_returns_zero(self, tmp_db):
        assert sdc.seed_from_disclosures() == {"seeded": 0, "skipped_existing": 0}

    def test_two_different_disclosures_both_seeded(self, tmp_db):
        _seed_disclosure(tmp_db, rcept_no="1", rcept_dt="2026-05-15")
        _seed_disclosure(tmp_db, rcept_no="2", rcept_dt="2026-06-01", report_nm="주요사항보고서")
        result = sdc.seed_from_disclosures()
        assert result["seeded"] == 2
