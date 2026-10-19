"""
collectors/dart_client.py 테스트 (DART 연동, 키 없이 구현).

**이 세션은 실제 DART API를 한 번도 호출하지 않는다.** 파싱/정규화 순수
함수는 tests/fixtures/의 가짜 응답으로 직접 검증하고, DartClient의
네트워크 호출은 requests/HTTP 레이어만 monkeypatch로 끊어서 검증한다.
실제 네트워크를 타는 테스트는 DART_LIVE_TEST=1일 때만 돌고(기본 skip),
그마저 키가 없으면 한 번 더 skip한다.
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors import dart_client as dc  # noqa: E402
from config import settings  # noqa: E402
from db import store  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _fake_settings(key: str = "", ready: bool | None = None) -> SimpleNamespace:
    """config.settings는 frozen dataclass라 속성을 직접 monkeypatch할 수 없다
    (FrozenInstanceError) — collectors/dart_client.py가 바인딩해둔 모듈 수준
    이름(dart_client.settings) 자체를 가짜 객체로 바꿔치기한다."""
    return SimpleNamespace(dart_api_key=key, dart_ready=(bool(key) if ready is None else ready))


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


class TestParseCorpCodeXml:
    def test_parses_rows_with_stock_code(self):
        xml_bytes = (FIXTURES / "dart_corp_code.zip").read_bytes()
        with zipfile.ZipFile(__import__("io").BytesIO(xml_bytes)) as zf:
            xml = zf.read("CORPCODE.xml")
        rows = dc.parse_corp_code_xml(xml)
        assert len(rows) == 3
        hynix = next(r for r in rows if r["stock_code"] == "000660")
        assert hynix["corp_code"] == "00164779"
        assert hynix["corp_name"] == "SK하이닉스"

    def test_unlisted_company_has_none_stock_code(self):
        xml_bytes = (FIXTURES / "dart_corp_code.zip").read_bytes()
        with zipfile.ZipFile(__import__("io").BytesIO(xml_bytes)) as zf:
            xml = zf.read("CORPCODE.xml")
        rows = dc.parse_corp_code_xml(xml)
        unlisted = next(r for r in rows if r["corp_code"] == "00999999")
        assert unlisted["stock_code"] is None  # 빈 문자열을 지어내지 않고 None


class TestCheckStatus:
    def test_ok_status_returns_list(self):
        raw = _load_fixture("dart_single_acnt_response.json")
        assert len(dc._check_status(raw)) == 3

    def test_no_data_status_returns_empty_list(self):
        raw = _load_fixture("dart_no_data_response.json")
        assert dc._check_status(raw) == []

    def test_unknown_error_status_raises(self):
        with pytest.raises(dc.DartApiError):
            dc._check_status({"status": "900", "message": "알수없는 오류"})


class TestRceptDtFromRceptNo:
    def test_valid_8_digit_prefix(self):
        assert dc._rcept_dt_from_rcept_no("20260515000123") == "2026-05-15"

    def test_none_when_missing(self):
        assert dc._rcept_dt_from_rcept_no(None) is None

    def test_none_when_not_date_like(self):
        assert dc._rcept_dt_from_rcept_no("99999999000123") is None

    def test_none_when_too_short(self):
        assert dc._rcept_dt_from_rcept_no("2026") is None


class TestNormalizeFinancialRows:
    def test_normalizes_standard_response(self):
        raw = _load_fixture("dart_single_acnt_response.json")["list"]
        rows = dc.normalize_financial_rows(raw, "00164779", "2026", "11013")
        assert len(rows) == 3
        equity = next(r for r in rows if r["account_nm"] == "자본총계")
        assert equity["thstrm_amount"] == 65000000000000.0
        assert equity["rcept_dt"] == "2026-05-15"
        assert equity["corp_code"] == "00164779"

    def test_missing_account_nm_row_is_dropped(self):
        raw = _load_fixture("dart_single_acnt_response_missing_fields.json")["list"]
        rows = dc.normalize_financial_rows(raw, "00164779", "2026", "11013")
        names = [r["account_nm"] for r in rows]
        assert "자본총계" in names and "영업이익" in names
        assert len(rows) == 2  # 계정명 없는 중간 행 제외

    def test_rcept_dt_derived_from_rcept_no_when_missing(self):
        raw = _load_fixture("dart_single_acnt_response_missing_fields.json")["list"]
        rows = dc.normalize_financial_rows(raw, "00164779", "2026", "11013")
        assert all(r["rcept_dt"] == "2026-05-15" for r in rows)

    def test_non_numeric_amount_becomes_none_not_fabricated(self):
        raw = _load_fixture("dart_single_acnt_response_missing_fields.json")["list"]
        rows = dc.normalize_financial_rows(raw, "00164779", "2026", "11013")
        op_income = next(r for r in rows if r["account_nm"] == "영업이익")
        assert op_income["thstrm_amount"] is None

    def test_comma_formatted_amount_parsed(self):
        raw = _load_fixture("dart_single_acnt_response_missing_fields.json")["list"]
        rows = dc.normalize_financial_rows(raw, "00164779", "2026", "11013")
        equity = next(r for r in rows if r["account_nm"] == "자본총계")
        assert equity["thstrm_amount"] == 65000000000000.0

    def test_empty_list_returns_empty(self):
        assert dc.normalize_financial_rows([], "00164779", "2026", "11013") == []


class TestNormalizeDisclosureRows:
    def test_normalizes_standard_response(self):
        raw = _load_fixture("dart_disclosure_list_response.json")["list"]
        rows = dc.normalize_disclosure_rows(raw)
        assert len(rows) == 2
        first = rows[0]
        assert first["rcept_no"] == "20260515000123"
        assert first["rcept_dt"] == "2026-05-15"
        assert first["stock_code"] == "000660"

    def test_row_without_rcept_no_is_dropped(self):
        rows = dc.normalize_disclosure_rows([{"corp_code": "x", "rcept_dt": "2026-01-01"}])
        assert rows == []

    def test_empty_list_returns_empty(self):
        assert dc.normalize_disclosure_rows([]) == []


class TestDartClientGetJson:
    def test_calls_endpoint_with_key_and_params(self, monkeypatch):
        import requests

        seen = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"status": "000", "message": "정상", "list": []}

        def fake_get(url, params=None, timeout=None):
            seen["url"] = url
            seen["params"] = params
            return FakeResponse()

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(dc, "settings", _fake_settings("SUPER-SECRET-KEY"))
        client = dc.DartClient()
        data = client._get_json("fnlttSinglAcnt.json", {"corp_code": "00164779"})

        assert data["status"] == "000"
        assert seen["params"]["crtfc_key"] == "SUPER-SECRET-KEY"
        assert seen["params"]["corp_code"] == "00164779"

    def test_request_exception_message_is_masked(self, monkeypatch):
        import requests

        def fake_get(url, params=None, timeout=None):
            raise requests.exceptions.HTTPError(
                f"404 Client Error: Not Found for url: {url}?crtfc_key=SUPER-SECRET-KEY"
            )

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(dc, "settings", _fake_settings("SUPER-SECRET-KEY"))
        client = dc.DartClient()
        with pytest.raises(dc.DartApiError) as exc_info:
            client._get_json("fnlttSinglAcnt.json", {})
        assert "SUPER-SECRET-KEY" not in str(exc_info.value)
        assert "***" in str(exc_info.value)


class TestDartClientRepr:
    def test_repr_never_shows_key(self, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings("SUPER-SECRET-KEY"))
        client = dc.DartClient()
        text = repr(client)
        assert "SUPER-SECRET-KEY" not in text
        assert str(client) == text


class TestCorpCodeMapCaching:
    def test_uses_cache_when_present(self, monkeypatch, tmp_path):
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"000660": "00164779", "005930": "00126380"}))
        monkeypatch.setattr(dc, "DART_CORP_CODE_CACHE", cache_path)

        def boom(*a, **k):
            raise AssertionError("캐시가 있는데 네트워크를 호출함")

        monkeypatch.setattr(dc.DartClient, "_get_bytes", boom)
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))

        client = dc.DartClient()
        result = client.corp_code_map(["000660"])
        assert result == {"000660": "00164779"}

    def test_downloads_and_caches_when_missing(self, monkeypatch, tmp_path):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr(dc, "DART_CORP_CODE_CACHE", cache_path)
        zip_bytes = (FIXTURES / "dart_corp_code.zip").read_bytes()
        monkeypatch.setattr(dc.DartClient, "_get_bytes", lambda self, path, params: zip_bytes)
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))

        client = dc.DartClient()
        result = client.corp_code_map(["000660", "005930"])
        assert result == {"000660": "00164779", "005930": "00126380"}
        assert cache_path.exists()  # 캐시 파일이 저장됨

    def test_unknown_stock_code_silently_absent(self, monkeypatch, tmp_path):
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"000660": "00164779"}))
        monkeypatch.setattr(dc, "DART_CORP_CODE_CACHE", cache_path)
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))

        client = dc.DartClient()
        result = client.corp_code_map(["999999"])
        assert result == {}  # 지어내지 않음


class TestCollectFinancialsCaching:
    def test_skips_network_when_report_already_exists(self, tmp_db, monkeypatch):
        store.upsert_financials([{
            "corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
            "account_nm": "자본총계", "rcept_dt": "2026-05-15",
        }], path=tmp_db)
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))

        def boom(*a, **k):
            raise AssertionError("이미 있는 보고서인데 다시 호출함")

        monkeypatch.setattr(dc.DartClient, "single_company_accounts", boom)
        n = dc.collect_financials("00164779", "2026", "11013")
        assert n == 0

    def test_force_refetches_even_if_exists(self, tmp_db, monkeypatch):
        store.upsert_financials([{
            "corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
            "account_nm": "자본총계", "rcept_dt": "2026-05-15",
        }], path=tmp_db)
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))
        raw = _load_fixture("dart_single_acnt_response.json")["list"]
        monkeypatch.setattr(dc.DartClient, "single_company_accounts", lambda self, *a, **k: raw)

        n = dc.collect_financials("00164779", "2026", "11013", force=True)
        assert n == 3

    def test_skips_entirely_when_no_key(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings(ready=False))

        def boom(*a, **k):
            raise AssertionError("키 없는데 호출함")

        monkeypatch.setattr(dc.DartClient, "single_company_accounts", boom)
        assert dc.collect_financials("00164779", "2026", "11013") == 0

    def test_stores_normalized_rows_on_first_fetch(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))
        raw = _load_fixture("dart_single_acnt_response.json")["list"]
        monkeypatch.setattr(dc.DartClient, "single_company_accounts", lambda self, *a, **k: raw)

        n = dc.collect_financials("00164779", "2026", "11013")
        assert n == 3
        df = store.get_financials("00164779", path=tmp_db)
        assert len(df) == 3


class TestExtractSharesOutstanding:
    def test_prefers_total_row(self):
        raw = _load_fixture("dart_stock_total_count_response.json")["list"]
        assert dc.extract_shares_outstanding(raw) == pytest.approx(728002365.0)

    def test_falls_back_to_first_row_when_no_total(self):
        raw = [{"se": "보통주", "istc_totqy": "100"}]
        assert dc.extract_shares_outstanding(raw) == pytest.approx(100.0)

    def test_none_when_no_candidate_field_matches(self):
        raw = [{"se": "합계", "unknown_field": "100"}]
        assert dc.extract_shares_outstanding(raw) is None

    def test_none_when_empty(self):
        assert dc.extract_shares_outstanding([]) is None


class TestCollectSharesOutstanding:
    def test_skips_entirely_when_no_key(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings(ready=False))

        def boom(*a, **k):
            raise AssertionError("키 없는데 호출함")

        monkeypatch.setattr(dc.DartClient, "stock_total_count", boom)
        assert dc.collect_shares_outstanding("00164779", "2026", "11013") == 0

    def test_stores_as_synthetic_financials_row(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))
        raw = _load_fixture("dart_stock_total_count_response.json")["list"]
        monkeypatch.setattr(dc.DartClient, "stock_total_count", lambda self, *a, **k: raw)

        n = dc.collect_shares_outstanding("00164779", "2026", "11013")
        assert n == 1
        df = store.get_financials("00164779", dc.SHARES_OUTSTANDING_ACCOUNT_NM, path=tmp_db)
        assert len(df) == 1
        assert df.iloc[0]["thstrm_amount"] == pytest.approx(728002365.0)
        assert str(df.iloc[0]["rcept_dt"].date()) == "2026-05-15"

    def test_skips_network_when_already_cached(self, tmp_db, monkeypatch):
        store.upsert_financials([{
            "corp_code": "00164779", "bsns_year": "2026", "reprt_code": "11013",
            "account_nm": dc.SHARES_OUTSTANDING_ACCOUNT_NM, "thstrm_amount": 1.0, "rcept_dt": "2026-05-15",
        }], path=tmp_db)
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))

        def boom(*a, **k):
            raise AssertionError("이미 있는데 다시 호출함")

        monkeypatch.setattr(dc.DartClient, "stock_total_count", boom)
        assert dc.collect_shares_outstanding("00164779", "2026", "11013") == 0

    def test_returns_zero_when_shares_not_extractable(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))
        monkeypatch.setattr(dc.DartClient, "stock_total_count", lambda self, *a, **k: [{"se": "합계", "unknown": "1"}])
        assert dc.collect_shares_outstanding("00164779", "2026", "11013") == 0


class TestCollectDisclosures:
    def test_skips_entirely_when_no_key(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings(ready=False))

        def boom(*a, **k):
            raise AssertionError("키 없는데 호출함")

        monkeypatch.setattr(dc.DartClient, "disclosure_list", boom)
        assert dc.collect_disclosures("00164779") == 0

    def test_stores_normalized_rows(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings("k"))
        raw = _load_fixture("dart_disclosure_list_response.json")["list"]
        monkeypatch.setattr(dc.DartClient, "disclosure_list", lambda self, *a, **k: raw)

        n = dc.collect_disclosures("00164779")
        assert n == 2
        df = store.get_disclosures("000660", path=tmp_db)
        assert len(df) == 2  # 두 공시 모두 000660(SK하이닉스) 소속


class TestCollectAll:
    def test_returns_zero_when_no_key(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dc, "settings", _fake_settings(ready=False))

        def boom(*a, **k):
            raise AssertionError("키 없는데 호출함")

        monkeypatch.setattr(dc.DartClient, "corp_code_map", boom)
        assert dc.collect_all() == 0


@pytest.mark.skipif(
    os.getenv("DART_LIVE_TEST") != "1",
    reason="실제 DART 네트워크 호출 테스트 — DART_LIVE_TEST=1일 때만 실행(기본 skip, 공통 규칙: 실제 외부 API 호출 금지)",
)
class TestLiveDartNetworkCall:
    def test_real_corp_code_call(self):
        if not settings.dart_ready:
            pytest.skip("DART_API_KEY가 .env에 없음 — 실키 없이는 실제 호출 테스트 불가")
        result = dc.DartClient().corp_code_map(["000660"])
        assert isinstance(result, dict)
