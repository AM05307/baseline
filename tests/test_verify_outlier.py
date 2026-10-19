"""
scripts/verify_outlier.py 테스트. 가짜 CSV와 임시 DB만 쓴다 — 실제
data/manual/ 폴더나 실제 수집 데이터는 건드리지 않는다.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from scripts import verify_outlier as vo  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _write_csv(tmp_path, name, content):
    f = tmp_path / name
    f.write_text(content)
    return f


class TestLoadManualCsvCloses:
    def test_parses_standard_english_columns(self, tmp_path):
        f = _write_csv(tmp_path, "a.csv", "date,close\n2001-11-09,138000\n2001-11-12,182000\n")
        s = vo.load_manual_csv_closes(f)
        assert len(s) == 2
        assert s.loc["2001-11-12"] == 182000

    def test_parses_korean_columns_with_commas(self, tmp_path):
        f = _write_csv(tmp_path, "b.csv", "일자,종가\n2001-11-09,\"138,000\"\n2001-11-12,\"182,000\"\n")
        s = vo.load_manual_csv_closes(f)
        assert s.loc["2001-11-12"] == 182000

    def test_unknown_columns_returns_empty_not_fabricated(self, tmp_path):
        f = _write_csv(tmp_path, "c.csv", "foo,bar\n1,2\n")
        s = vo.load_manual_csv_closes(f)
        assert s.empty

    def test_ignores_extra_columns(self, tmp_path):
        f = _write_csv(tmp_path, "d.csv", "date,open,high,low,close,volume\n2001-11-12,1,1,1,182000,999\n")
        s = vo.load_manual_csv_closes(f)
        assert s.loc["2001-11-12"] == 182000


class TestFindManualReturn:
    def test_finds_return_from_first_matching_csv(self, tmp_path):
        _write_csv(tmp_path, "a.csv", "date,close\n2001-11-09,138000\n2001-11-12,182000\n")
        r, fname = vo.find_manual_return("000660", "2001-11-12", tmp_path)
        assert r == pytest.approx((182000 / 138000 - 1) * 100)
        assert fname == "a.csv"

    def test_returns_none_when_dir_missing(self, tmp_path):
        r, fname = vo.find_manual_return("000660", "2001-11-12", tmp_path / "nope")
        assert r is None and fname is None

    def test_returns_none_when_no_csv_has_the_date(self, tmp_path):
        _write_csv(tmp_path, "a.csv", "date,close\n2026-01-01,100\n2026-01-02,101\n")
        r, fname = vo.find_manual_return("000660", "2001-11-12", tmp_path)
        assert r is None and fname is None


class TestCheckOutlier:
    def test_match_within_tolerance(self, tmp_db, tmp_path):
        store.upsert_prices([
            {"ticker": "000660", "date": "2001-11-09", "open": 1, "high": 1, "low": 1, "close": 138000, "volume": 1, "source": "test"},
            {"ticker": "000660", "date": "2001-11-12", "open": 1, "high": 1, "low": 1, "close": 182000, "volume": 1, "source": "test"},
        ], path=tmp_db)
        _write_csv(tmp_path, "a.csv", "date,close\n2001-11-09,138000\n2001-11-12,182000\n")

        result = vo.check_outlier("000660", "2001-11-12", manual_dir=tmp_path, db_path=tmp_db)
        assert result.match is True
        assert result.diff_pct_points == pytest.approx(0.0)

    def test_mismatch_when_values_differ(self, tmp_db, tmp_path):
        store.upsert_prices([
            {"ticker": "000660", "date": "2001-11-09", "open": 1, "high": 1, "low": 1, "close": 138000, "volume": 1, "source": "test"},
            {"ticker": "000660", "date": "2001-11-12", "open": 1, "high": 1, "low": 1, "close": 182000, "volume": 1, "source": "test"},
        ], path=tmp_db)
        _write_csv(tmp_path, "a.csv", "date,close\n2001-11-09,138000\n2001-11-12,145000\n")  # 공식 데이터는 다른 값

        result = vo.check_outlier("000660", "2001-11-12", manual_dir=tmp_path, db_path=tmp_db)
        assert result.match is False
        assert result.diff_pct_points > 1.0  # 꽤 큰 차이

    def test_no_comparison_when_csv_missing(self, tmp_db, tmp_path):
        store.upsert_prices([
            {"ticker": "000660", "date": "2001-11-09", "open": 1, "high": 1, "low": 1, "close": 138000, "volume": 1, "source": "test"},
            {"ticker": "000660", "date": "2001-11-12", "open": 1, "high": 1, "low": 1, "close": 182000, "volume": 1, "source": "test"},
        ], path=tmp_db)
        result = vo.check_outlier("000660", "2001-11-12", manual_dir=tmp_path, db_path=tmp_db)
        assert result.match is None
        assert result.csv_return_pct is None

    def test_no_comparison_when_db_missing(self, tmp_db, tmp_path):
        _write_csv(tmp_path, "a.csv", "date,close\n2001-11-09,138000\n2001-11-12,182000\n")
        result = vo.check_outlier("000660", "2001-11-12", manual_dir=tmp_path, db_path=tmp_db)
        assert result.match is None
        assert result.db_return_pct is None

    def test_does_not_mutate_db(self, tmp_db, tmp_path):
        store.upsert_prices([
            {"ticker": "000660", "date": "2001-11-09", "open": 1, "high": 1, "low": 1, "close": 138000, "volume": 1, "source": "test"},
            {"ticker": "000660", "date": "2001-11-12", "open": 1, "high": 1, "low": 1, "close": 182000, "volume": 1, "source": "test"},
        ], path=tmp_db)
        _write_csv(tmp_path, "a.csv", "date,close\n2001-11-09,138000\n2001-11-12,145000\n")  # 불일치
        vo.check_outlier("000660", "2001-11-12", manual_dir=tmp_path, db_path=tmp_db)
        # 불일치가 나와도 DB 값은 그대로(자동 보정 없음)
        after = store.get_prices("000660", path=tmp_db)
        assert after.loc["2001-11-12"] == 182000


class TestScanAllTickersForLimitExceedances:
    def test_flags_exceedance_for_given_tickers(self, tmp_db):
        store.upsert_prices([
            {"ticker": "A", "date": "2020-01-02", "open": 1, "high": 1, "low": 1, "close": 100, "volume": 1, "source": "test"},
            {"ticker": "A", "date": "2020-01-03", "open": 1, "high": 1, "low": 1, "close": 145, "volume": 1, "source": "test"},  # +45%
            {"ticker": "B", "date": "2020-01-02", "open": 1, "high": 1, "low": 1, "close": 100, "volume": 1, "source": "test"},
            {"ticker": "B", "date": "2020-01-03", "open": 1, "high": 1, "low": 1, "close": 110, "volume": 1, "source": "test"},
        ], path=tmp_db)
        out = vo.scan_all_tickers_for_limit_exceedances(tickers=["A", "B"], db_path=tmp_db)
        assert {e.ticker for e in out} == {"A"}

    def test_empty_db_returns_empty(self, tmp_db):
        assert vo.scan_all_tickers_for_limit_exceedances(tickers=["000660", "005930"], db_path=tmp_db) == []

    def test_default_scope_is_krx_equities_only(self, tmp_db):
        # 지수/선물처럼 가격제한이 없는 티커가 DB에 있어도 기본 스캔 대상(KRX 개별종목)엔 안 걸린다
        store.upsert_prices([
            {"ticker": "US10Y", "date": "2020-01-02", "open": 1, "high": 1, "low": 1, "close": 2.0, "volume": 1, "source": "test"},
            {"ticker": "US10Y", "date": "2020-01-03", "open": 1, "high": 1, "low": 1, "close": 4.0, "volume": 1, "source": "test"},  # +100%
        ], path=tmp_db)
        out = vo.scan_all_tickers_for_limit_exceedances(db_path=tmp_db)  # 기본값(KRX_EQUITY_TICKERS)
        assert out == []  # US10Y는 스캔 대상에 없음(000660/005930이 DB에 없으므로 전체가 빈 결과)


class TestMainCli:
    def test_runs_without_crashing_on_default_known_outliers(self, tmp_db, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["verify_outlier.py"])
        sys.modules.pop("scripts.verify_outlier", None)
        runpy.run_module("scripts.verify_outlier", run_name="__main__")
        out = capsys.readouterr().out
        assert "000660" in out
        assert "2001-11-12" in out
        for phrase in ["오를 것", "매수 시점", "매도 시점"]:
            assert phrase not in out

    def test_custom_ticker_and_date_requires_both(self, tmp_db, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["verify_outlier.py", "--ticker", "005930"])
        sys.modules.pop("scripts.verify_outlier", None)
        with pytest.raises(SystemExit):
            runpy.run_module("scripts.verify_outlier", run_name="__main__")

    def test_scan_limits_flag_runs_scan(self, tmp_db, monkeypatch, capsys):
        store.upsert_prices([
            {"ticker": "000660", "date": "2020-01-02", "open": 1, "high": 1, "low": 1, "close": 100, "volume": 1, "source": "test"},
            {"ticker": "000660", "date": "2020-01-03", "open": 1, "high": 1, "low": 1, "close": 145, "volume": 1, "source": "test"},
        ], path=tmp_db)
        monkeypatch.setattr(sys, "argv", ["verify_outlier.py", "--scan-limits"])
        sys.modules.pop("scripts.verify_outlier", None)
        runpy.run_module("scripts.verify_outlier", run_name="__main__")
        out = capsys.readouterr().out
        assert "가격제한 초과 스캔" in out
        assert "[000660] 2020-01-03" in out

    def test_scan_limits_all_flag_scans_every_ticker(self, tmp_db, monkeypatch, capsys):
        store.upsert_prices([
            {"ticker": "US10Y", "date": "2020-01-02", "open": 1, "high": 1, "low": 1, "close": 2.0, "volume": 1, "source": "test"},
            {"ticker": "US10Y", "date": "2020-01-03", "open": 1, "high": 1, "low": 1, "close": 4.0, "volume": 1, "source": "test"},
        ], path=tmp_db)
        monkeypatch.setattr(sys, "argv", ["verify_outlier.py", "--scan-limits-all"])
        sys.modules.pop("scripts.verify_outlier", None)
        runpy.run_module("scripts.verify_outlier", run_name="__main__")
        out = capsys.readouterr().out
        assert "[US10Y] 2020-01-03" in out
