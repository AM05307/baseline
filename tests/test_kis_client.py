"""
collectors/kis_client.py 테스트 (4단계: KIS 키 없이 미리 구현).

**이 세션은 실제 KIS API를 한 번도 호출하지 않는다** — 공통 규칙("실제
외부 API는 호출하지 마")과 이 프로젝트 CLAUDE.md 보안 원칙 때문이다.
parse_investor_flow_response()는 순수 함수라 tests/fixtures/의 가짜 응답
JSON으로 직접 검증하고, investor_flow()·collect_investor_flow()는
requests/네트워크 쪽만 monkeypatch로 끊어서 오케스트레이션(저장까지
이어지는지, 키 없으면 호출 자체를 안 하는지)만 검증한다.

실제 네트워크를 타는 테스트는 `KIS_LIVE_TEST=1` 환경변수가 있을 때만
실행되고(기본은 skip) 그 테스트도 **키가 없으면 한 번 더 skip**된다 —
요청대로 "환경변수가 있을 때만 돌게, 없으면 skip".
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors import kis_client  # noqa: E402
from config import settings  # noqa: E402
from db import store  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fake_settings(key: str = "", secret: str = "", ready: bool | None = None) -> SimpleNamespace:
    """
    config.settings는 frozen dataclass라 속성을 직접 monkeypatch할 수 없다
    (FrozenInstanceError) — 그래서 collectors/kis_client.py가 `from config
    import settings`로 바인딩해둔 모듈 수준 이름(kis_client.settings) 자체를
    이 가짜 객체로 통째로 바꿔치기하는 방식을 쓴다.
    """
    return SimpleNamespace(
        kis_app_key=key, kis_app_secret=secret,
        kis_ready=(bool(key and secret) if ready is None else ready),
    )


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


class TestParseInvestorFlowResponse:
    def test_normalizes_rows_to_upsert_flow_shape(self):
        raw = _load_fixture("kis_investor_flow_response.json")
        rows = kis_client.parse_investor_flow_response(raw, "000660")

        assert len(rows) == 3
        assert rows[0] == {
            "ticker": "000660", "date": "2026-10-02",
            "individual": -1200, "foreigner": -5300, "institution": 6500,
        }
        assert rows[2] == {
            "ticker": "000660", "date": "2026-10-06",
            "individual": -900, "foreigner": 2200, "institution": -1300,
        }

    def test_dates_converted_from_yyyymmdd_to_iso(self):
        raw = _load_fixture("kis_investor_flow_response.json")
        rows = kis_client.parse_investor_flow_response(raw, "000660")
        assert all(len(r["date"]) == 10 and r["date"][4] == "-" and r["date"][7] == "-" for r in rows)

    def test_missing_field_becomes_none_not_fabricated(self):
        raw = _load_fixture("kis_investor_flow_response_missing_fields.json")
        rows = kis_client.parse_investor_flow_response(raw, "000660")
        # row 1: individual 필드가 빈 문자열 -> None (0으로 지어내지 않음)
        row1 = next(r for r in rows if r["date"] == "2026-10-02")
        assert row1["individual"] is None
        assert row1["foreigner"] == -5300

    def test_row_without_date_is_skipped(self):
        raw = _load_fixture("kis_investor_flow_response_missing_fields.json")
        rows = kis_client.parse_investor_flow_response(raw, "000660")
        assert len(rows) == 2  # 날짜 없는 중간 행은 제외
        assert all(r["date"] for r in rows)

    def test_non_numeric_field_becomes_none(self):
        raw = _load_fixture("kis_investor_flow_response_missing_fields.json")
        rows = kis_client.parse_investor_flow_response(raw, "000660")
        row3 = next(r for r in rows if r["date"] == "2026-10-06")
        assert row3["institution"] is None  # "not_a_number" -> None

    def test_output_not_a_list_returns_empty(self):
        rows = kis_client.parse_investor_flow_response({"output": "unexpected"}, "000660")
        assert rows == []

    def test_missing_output_key_returns_empty(self):
        rows = kis_client.parse_investor_flow_response({"rt_cd": "0"}, "000660")
        assert rows == []


class TestInvestorFlowMethod:
    def test_calls_get_with_expected_params_and_returns_parsed_rows(self, monkeypatch):
        raw = _load_fixture("kis_investor_flow_response.json")
        seen = {}

        def fake_get(self, path, tr_id, params):
            seen["path"] = path
            seen["tr_id"] = tr_id
            seen["params"] = params
            return raw

        monkeypatch.setattr(kis_client.KisClient, "_get", fake_get)
        client = kis_client.KisClient(paper=True)
        rows = client.investor_flow("000660")

        assert seen["params"] == {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "000660"}
        assert seen["tr_id"] == kis_client.INVESTOR_FLOW_TR_ID
        assert seen["path"] == kis_client.INVESTOR_FLOW_PATH
        assert len(rows) == 3


class TestCollectInvestorFlowGuardedByKeyPresence:
    def test_skips_network_entirely_when_no_key(self, tmp_db, monkeypatch):
        monkeypatch.setattr(kis_client, "settings", _fake_settings(ready=False))
        # investor_flow 자체가 호출되면 실패하도록 — 키 없을 때 호출 안 되는지 확인
        monkeypatch.setattr(
            kis_client.KisClient, "investor_flow",
            lambda self, code="000660": (_ for _ in ()).throw(AssertionError("키 없는데 호출됨")),
        )
        n = kis_client.collect_investor_flow("000660")
        assert n == 0

    def test_stores_rows_via_upsert_flow_when_key_present(self, tmp_db, monkeypatch):
        raw = _load_fixture("kis_investor_flow_response.json")
        monkeypatch.setattr(kis_client, "settings", _fake_settings("fake-key-for-test", "fake-secret-for-test"))
        monkeypatch.setattr(
            kis_client.KisClient, "investor_flow",
            lambda self, code="000660": kis_client.parse_investor_flow_response(raw, code),
        )

        n = kis_client.collect_investor_flow("000660")
        assert n == 3

        flow = store.get_flow("000660", days=10, path=tmp_db)
        assert len(flow) == 3


class TestIdempotentStorage:
    def test_rerunning_same_data_does_not_duplicate_rows(self, tmp_db, monkeypatch):
        raw = _load_fixture("kis_investor_flow_response.json")
        monkeypatch.setattr(kis_client, "settings", _fake_settings("fake-key-for-test", "fake-secret-for-test"))
        monkeypatch.setattr(
            kis_client.KisClient, "investor_flow",
            lambda self, code="000660": kis_client.parse_investor_flow_response(raw, code),
        )

        kis_client.collect_investor_flow("000660")
        kis_client.collect_investor_flow("000660")  # 같은 날 재실행

        flow = store.get_flow("000660", days=10, path=tmp_db)
        assert len(flow) == 3  # ON CONFLICT 멱등 — 중복 안 생김

    def test_rerun_with_updated_values_overwrites_not_duplicates(self, tmp_db):
        rows_v1 = [{"ticker": "000660", "date": "2026-10-06", "individual": -900, "foreigner": 100, "institution": 800}]
        rows_v2 = [{"ticker": "000660", "date": "2026-10-06", "individual": -900, "foreigner": 9999, "institution": 800}]

        store.upsert_flow(rows_v1, path=tmp_db)
        store.upsert_flow(rows_v2, path=tmp_db)

        flow = store.get_flow("000660", days=10, path=tmp_db)
        assert len(flow) == 1
        assert int(flow.iloc[0]["foreigner"]) == 9999  # 최신 값으로 덮어씀


class TestSecretsNeverLeak:
    def test_repr_does_not_contain_key_or_secret_values(self, monkeypatch):
        monkeypatch.setattr(kis_client, "settings", _fake_settings("SUPER-SECRET-KEY", "SUPER-SECRET-SECRET"))
        client = kis_client.KisClient(paper=True)
        text = repr(client)
        assert "SUPER-SECRET-KEY" not in text
        assert "SUPER-SECRET-SECRET" not in text
        assert str(client) == text  # __str__도 같은 안전한 repr을 씀

    def test_repr_does_not_contain_cached_token(self, monkeypatch):
        monkeypatch.setattr(kis_client, "settings", _fake_settings("k", "s"))
        client = kis_client.KisClient(paper=True)
        client._token = "SUPER-SECRET-TOKEN-VALUE"
        assert "SUPER-SECRET-TOKEN-VALUE" not in repr(client)

    def test_investor_flow_params_never_include_key_or_secret(self, monkeypatch):
        """조회 파라미터(쿼리스트링)에 앱키/시크릿이 섞여 들어가지 않는지 — 그건
        헤더(_headers())에만 있어야 한다. params에 들어가면 요청 로그·에러
        메시지에 노출될 위험이 커진다."""
        monkeypatch.setattr(kis_client, "settings", _fake_settings("SUPER-SECRET-KEY", "SUPER-SECRET-SECRET"))
        seen = {}

        def fake_get(self, path, tr_id, params):
            seen["params"] = params
            return {"output": []}

        monkeypatch.setattr(kis_client.KisClient, "_get", fake_get)
        kis_client.KisClient(paper=True).investor_flow("000660")

        assert "SUPER-SECRET-KEY" not in json.dumps(seen["params"])
        assert "SUPER-SECRET-SECRET" not in json.dumps(seen["params"])


class TestNoOrderEndpoints:
    def test_client_has_no_order_buy_sell_methods(self):
        """원칙 2(매매 기능 미구현) — 주문 관련 메서드가 아예 없는지 이름으로 확인."""
        forbidden_substrings = ["order", "buy", "sell", "매수", "매도"]
        method_names = [name for name in dir(kis_client.KisClient) if not name.startswith("_")]
        for name in method_names:
            lowered = name.lower()
            for bad in forbidden_substrings:
                assert bad not in lowered, f"주문처럼 보이는 메서드 발견: {name}"


@pytest.mark.skipif(
    os.getenv("KIS_LIVE_TEST") != "1",
    reason="실제 KIS 네트워크 호출 테스트 — KIS_LIVE_TEST=1일 때만 실행(기본 skip, 공통 규칙: 실제 외부 API 호출 금지)",
)
class TestLiveInvestorFlowNetworkCall:
    def test_real_investor_flow_call(self):
        if not settings.kis_ready:
            pytest.skip("KIS_APP_KEY/SECRET이 .env에 없음 — 실키 없이는 실제 호출 테스트 불가")
        rows = kis_client.KisClient(paper=True).investor_flow("000660")
        assert isinstance(rows, list)
