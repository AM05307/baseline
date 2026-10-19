"""
KisBroker(모의투자 전용) — 가짜 HTTP 세션으로만 검증(네트워크 없음). 실제 모의투자 호출은 KIS_PAPER_TEST=1일 때만.

시작 단계 거부(실전 URL·다른 호스트·키 섞임·누락·형식), 요청 구성(경로·tr_id·필드), 응답 파싱, 비밀값 마스킹.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import engine, store  # noqa: E402
from executor.broker import kis  # noqa: E402
from executor.broker.base import BrokerError  # noqa: E402
from executor.tiers import parse_ladder  # noqa: E402

KEY, SECRET, ACCT, TOKEN = "PAPERKEY123456", "PAPERSECRET987654", "50001234", "TOKENABCDEF999"
GOOD = kis.KisPaperSettings(KEY, SECRET, ACCT, "01")


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


class FakeSession:
    """요청을 기록하고, 경로별로 미리 정한 응답을 돌려준다."""

    def __init__(self, responses: dict | None = None, raise_on: str | None = None):
        self.calls = []
        self.responses = {kis.TOKEN_PATH: [{"access_token": TOKEN}], **(responses or {})}
        self.raise_on = raise_on

    def _respond(self, method, url, **kw):
        self.calls.append((method, url, kw))
        path = url.replace(kis.PAPER_BASE_URL, "")
        if self.raise_on and self.raise_on in path:
            raise ConnectionError(f"connect failed {url} appkey={KEY} Bearer {TOKEN}")
        queue = self.responses[path]
        return FakeResp(queue.pop(0) if len(queue) > 1 else queue[0])

    def post(self, url, **kw):
        return self._respond("POST", url, **kw)

    def get(self, url, **kw):
        return self._respond("GET", url, **kw)


def _broker(session=None, live=frozenset({"LIVEKEY000000", "LIVESECRET0000"})):
    return kis.KisBroker(GOOD, live=set(live), session=session or FakeSession())


class TestStartupGuards:
    def test_good_paper_settings_start(self):
        assert _broker() is not None

    @pytest.mark.parametrize("url", ["https://openapi.koreainvestment.com:9443", "http://openapivts.koreainvestment.com:29443",
                                     "https://example.com", ""])
    def test_non_paper_url_refused(self, url):
        s = kis.KisPaperSettings(KEY, SECRET, ACCT, "01", base_url=url)
        with pytest.raises(kis.ConfigError):
            kis.KisBroker(s, live=set(), session=FakeSession())

    def test_live_key_mixed_in_is_refused_without_leaking_it(self):
        with pytest.raises(kis.ConfigError) as e:
            kis.KisBroker(GOOD, live={KEY}, session=FakeSession())
        assert KEY not in str(e.value)

    @pytest.mark.parametrize("s", [kis.KisPaperSettings("", SECRET, ACCT, "01"), kis.KisPaperSettings(KEY, SECRET, "", "01"),
                                   kis.KisPaperSettings(KEY, SECRET, "1234", "01"), kis.KisPaperSettings(KEY, SECRET, ACCT, "1")])
    def test_missing_or_malformed_refused(self, s):
        with pytest.raises(kis.ConfigError):
            kis.KisBroker(s, live=set(), session=FakeSession())

    def test_settings_load_from_paper_env_file_and_split_account(self, tmp_path):
        f = tmp_path / ".env.paper"
        f.write_text(f"KIS_PAPER_APP_KEY={KEY}\nKIS_PAPER_APP_SECRET={SECRET}\nKIS_PAPER_ACCOUNT={ACCT}-01\n")
        s = kis.load_paper_settings(env={}, env_file=f)
        assert (s.account, s.product_code, s.base_url) == (ACCT, "01", kis.PAPER_BASE_URL)

    def test_live_secrets_reads_only_live_names(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KIS_APP_KEY=LKEY1\nKIS_APP_SECRET=LSEC1\nOTHER=x\n")  # 가짜 값(훅 기준 10자 미만)
        assert kis.live_secrets(env={}, env_file=f) == {"LKEY1", "LSEC1"}

    def test_repr_never_shows_secrets(self):
        b = _broker()
        for text in (repr(b), repr(GOOD), str(GOOD)):
            assert KEY not in text and SECRET not in text and ACCT not in text


BAL = {"rt_cd": "0", "output1": [{"pdno": "000660", "hldg_qty": "12"}, {"pdno": "005930", "hldg_qty": "0"}],
       "output2": [{"tot_evlu_amt": "10500000", "dnca_tot_amt": "8000000"}]}


class TestRequests:
    def test_buy_and_sell_use_paper_tr_ids_and_official_fields(self):
        sess = FakeSession({kis.ORDER_CASH_PATH: [{"rt_cd": "0", "output": {"ODNO": "0001", "KRX_FWDG_ORD_ORGNO": "06010"}}]})
        b = _broker(sess)
        st = b.place_order("000660", "buy", 3, 100_000)
        assert st.broker_order_id == "06010:0001" and st.status == "submitted"
        b.place_order("000660", "sell", 2, 100_000)
        orders = [c for c in sess.calls if c[1].endswith(kis.ORDER_CASH_PATH)]
        assert all(c[1].startswith(kis.PAPER_BASE_URL) for c in sess.calls)
        assert [c[2]["headers"]["tr_id"] for c in orders] == ["VTTC0012U", "VTTC0011U"]
        body = orders[0][2]["json"]
        assert body == {"CANO": ACCT, "ACNT_PRDT_CD": "01", "PDNO": "000660", "ORD_DVSN": "01", "ORD_QTY": "3",
                        "ORD_UNPR": "0", "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "", "CNDT_PRIC": ""}
        assert orders[1][2]["json"]["SLL_TYPE"] == "01"
        assert sum(1 for c in sess.calls if c[1].endswith(kis.TOKEN_PATH)) == 1  # 토큰은 한 번만

    def test_balance_and_positions_parse(self):
        b = _broker(FakeSession({kis.BALANCE_PATH: [BAL]}))
        assert b.get_positions() == {"000660": 12}
        bal = b.get_balance()
        assert (bal.total_value, bal.cash) == (10_500_000, 8_000_000)

    def test_order_status_and_cancel(self):
        ccld = {"rt_cd": "0", "output1": [{"odno": "0000000001", "ord_qty": "5", "tot_ccld_qty": "2", "avg_prvs": "99000",
                                            "cncl_yn": "N", "cnc_cfrm_qty": "0"}]}
        cancelled = {"rt_cd": "0", "output1": [{"odno": "1", "ord_qty": "5", "tot_ccld_qty": "2", "avg_prvs": "99000",
                                                 "cncl_yn": "Y", "cnc_cfrm_qty": "3"}]}
        sess = FakeSession({kis.DAILY_CCLD_PATH: [ccld, cancelled], kis.ORDER_CANCEL_PATH: [{"rt_cd": "0", "output": {}}]})
        b = _broker(sess)
        st = b.get_order_status("06010:1")
        assert (st.status, st.filled_qty, st.avg_price) == ("partial", 2, 99_000)
        st = b.cancel_order("06010:1")
        assert (st.status, st.filled_qty) == ("partial", 2)
        cancel = next(c for c in sess.calls if c[1].endswith(kis.ORDER_CANCEL_PATH))
        assert cancel[2]["headers"]["tr_id"] == "VTTC0013U" and cancel[2]["json"]["RVSE_CNCL_DVSN_CD"] == "02"

    def test_list_orders_parses_open_filled_and_cancelled(self):
        body = {"rt_cd": "0", "output1": [
            {"odno": "0000000007", "ord_gno_brno": "06010", "pdno": "000660", "sll_buy_dvsn_cd": "02", "ord_qty": "5",
             "tot_ccld_qty": "5", "avg_prvs": "100000", "cncl_yn": "N", "cnc_cfrm_qty": "0", "rmn_qty": "0"},
            {"odno": "8", "ord_gno_brno": "06010", "pdno": "000660", "sll_buy_dvsn_cd": "01", "ord_qty": "4",
             "tot_ccld_qty": "1", "avg_prvs": "99000", "cncl_yn": "N", "cnc_cfrm_qty": "0", "rmn_qty": "3"},
            {"odno": "9", "pdno": "069500", "sll_buy_dvsn_cd": "02", "ord_qty": "4", "tot_ccld_qty": "2",
             "avg_prvs": "40000", "cncl_yn": "Y", "cnc_cfrm_qty": "2"}]}
        sess = FakeSession({kis.DAILY_CCLD_PATH: [body]})
        orders = _broker(sess).list_orders("2026-10-15")
        assert [(o.side, o.filled_qty, o.open_qty, o.status) for o in orders] == [
            ("buy", 5, 0, "filled"), ("sell", 1, 3, "partial"), ("buy", 2, 0, "partial")]
        call = next(c for c in sess.calls if c[1].endswith(kis.DAILY_CCLD_PATH))
        assert call[2]["headers"]["tr_id"] == "VTTC0081R" and call[2]["params"]["INQR_STRT_DT"] == "20261015"

    def test_non_paper_tr_id_is_refused_even_internally(self):
        with pytest.raises(BrokerError):
            _broker()._call("POST", kis.ORDER_CASH_PATH, "TTTC0012U", {})


class TestSecretsNeverLeak:
    def test_api_error_message_is_redacted(self):
        bad = {"rt_cd": "1", "msg1": f"invalid {KEY} for account {ACCT}"}
        b = _broker(FakeSession({kis.BALANCE_PATH: [bad]}))
        with pytest.raises(BrokerError) as e:
            b.get_balance()
        assert KEY not in str(e.value) and ACCT not in str(e.value) and "[마스킹]" in str(e.value)

    def test_network_exception_with_token_in_message_is_redacted(self):
        b = _broker(FakeSession(raise_on=kis.BALANCE_PATH))
        with pytest.raises(BrokerError) as e:
            b.get_positions()
        msg = str(e.value)
        assert TOKEN not in msg and KEY not in msg and e.value.__cause__ is None

    def test_engine_event_log_has_no_secrets(self, tmp_path, monkeypatch):
        db = tmp_path / "e.db"
        monkeypatch.setattr(store, "DB_PATH", db)
        store.init_db(db)
        ladder = parse_ladder({"tiers": [{"name": "shadow", "kind": "virtual", "max_capital_krw": 1_000_000, "confirmed": True},
                                         {"name": "paper", "kind": "virtual", "max_capital_krw": 1_000_000, "confirmed": True}]})
        store.append_tier_event("2026-10-16", "init", None, "paper", "auto", path=db)
        b = _broker(FakeSession(raise_on=kis.BALANCE_PATH))
        r = engine.run_once(b, ladder, "2026-10-16", {"000660": 1.0}, {"000660": 100_000}, path=db,
                            kill_switch_file=tmp_path / "KS")
        assert r.status == "api_error"
        dump = store.events(path=db).to_string() + store.snapshots("paper", path=db).to_string()
        assert KEY not in dump and TOKEN not in dump and ACCT not in dump


@pytest.mark.skipif(os.environ.get("KIS_PAPER_TEST") != "1", reason="실제 모의투자 호출은 KIS_PAPER_TEST=1일 때만")
def test_live_paper_balance_read_only():
    """모의투자 서버에 잔고 조회만 한다(주문 없음). .env.paper의 KIS_PAPER_* 필요."""
    b = kis.KisBroker(kis.load_paper_settings())
    bal = b.get_balance()
    assert bal.total_value >= 0
