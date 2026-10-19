"""
KisBroker — 한국투자증권 Open API **모의투자 전용** 어댑터(골격).

범위(2026-10-18 사용자 결정): 모의투자 주문까지만. 이 파일에는 실전 URL로 요청을 보내는 경로가 없고, 실전 tr_id
(T로 시작)도 쓰지 않는다. 시작 단계(__init__)에서 아래 중 하나면 ConfigError로 멈춘다:
  - base URL이 모의투자 URL이 아니다(실전 URL·다른 호스트·http)
  - 모의투자 앱키·시크릿이 실전 키(KIS_APP_KEY·KIS_APP_SECRET)와 같다(키 섞임)
  - 키·시크릿·계좌번호가 비어 있거나 형식이 틀렸다

설정은 실전 설정과 분리한다: `.env.paper`(gitignore의 `.env.*`로 이미 제외) 또는 환경변수 KIS_PAPER_*.
실전 키는 "섞였는지" 비교할 때만 메모리에서 읽고, 출력·저장하지 않는다.

tr_id·경로·필드명 출처: koreainvestment/open-trading-api(commit 2077773, 2026-10-14)의 examples_llm/domestic_stock
샘플(order_cash·inquire_balance·order_rvsecncl·inquire_daily_ccld)과 kis_devlp.yaml·kis_auth.py. 코드는 복사하지
않았고 사실(경로·tr_id·필드 이름)만 맞췄다. **모의투자 서버에 실제로 호출해 본 적은 없다**(KIS_PAPER_TEST=1일 때만 도는
테스트가 있다).

보안: 키·시크릿·토큰·계좌번호는 Redactor로 모든 예외 메시지에서 지운다. repr에는 설정 여부만 나온다. 토큰은 메모리에만
둔다(파일로 저장하지 않음).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from executor.broker.base import Balance, Broker, BrokerError, BrokerOrder, OrderStatus
from executor.redact import Redactor, mask

ROOT = Path(__file__).resolve().parent.parent.parent
PAPER_ENV_FILE = ROOT / ".env.paper"
LIVE_ENV_FILE = ROOT / ".env"

PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"  # kis_devlp.yaml "vps"
LIVE_HOST_MARK = "openapi.koreainvestment.com"  # 실전 호스트(kis_devlp.yaml "prod") — 이 문자열이 보이면 거부

TOKEN_PATH = "/oauth2/tokenP"
ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
ORDER_CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
DAILY_CCLD_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"

# 모의투자 tr_id(V로 시작). 실전 tr_id(TTTC…)는 이 파일에 두지 않는다.
TR_BUY = "VTTC0012U"
TR_SELL = "VTTC0011U"
TR_CANCEL = "VTTC0013U"
TR_BALANCE = "VTTC8434R"
TR_DAILY_CCLD = "VTTC0081R"  # 3개월 이내 주문체결 조회
PAPER_TR_IDS = (TR_BUY, TR_SELL, TR_CANCEL, TR_BALANCE, TR_DAILY_CCLD)


class ConfigError(Exception):
    """시작 단계 설정 오류 — 이 예외가 나면 브로커를 만들지 않는다(종료). 메시지에 비밀값을 넣지 않는다."""


@dataclass(frozen=True, repr=False)
class KisPaperSettings:
    app_key: str
    app_secret: str
    account: str  # 종합계좌번호 8자리(CANO)
    product_code: str  # 계좌상품코드 2자리(ACNT_PRDT_CD)
    base_url: str = PAPER_BASE_URL

    def __repr__(self) -> str:
        return (f"KisPaperSettings(app_key={mask(self.app_key)}, app_secret={mask(self.app_secret)}, "
                f"account={mask(self.account)}, base_url={self.base_url})")


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(path).items() if v}


def load_paper_settings(env: Mapping[str, str] | None = None, env_file: Path = PAPER_ENV_FILE) -> KisPaperSettings:
    """KIS_PAPER_* 를 환경변수 → .env.paper 순으로 찾는다. 실전용 KIS_APP_KEY 등은 여기서 읽지 않는다."""
    src = {**_read_env_file(env_file), **dict(env if env is not None else os.environ)}
    account = src.get("KIS_PAPER_ACCOUNT", "")
    cano, prod = (account.split("-", 1) + [""])[:2] if "-" in account else (account, src.get("KIS_PAPER_PRODUCT_CODE", "01"))
    return KisPaperSettings(
        app_key=src.get("KIS_PAPER_APP_KEY", ""), app_secret=src.get("KIS_PAPER_APP_SECRET", ""),
        account=cano, product_code=prod or "01", base_url=src.get("KIS_PAPER_BASE_URL", PAPER_BASE_URL),
    )


def live_secrets(env: Mapping[str, str] | None = None, env_file: Path = LIVE_ENV_FILE) -> set[str]:
    """실전 키·시크릿 — 섞였는지 비교하는 데만 쓴다(반환값을 출력·저장하지 않는다)."""
    src = {**_read_env_file(env_file), **dict(env if env is not None else os.environ)}
    return {v for k in ("KIS_APP_KEY", "KIS_APP_SECRET") if (v := src.get(k))}


def validate_paper_settings(s: KisPaperSettings, live: set[str]) -> None:
    url = (s.base_url or "").rstrip("/")
    if LIVE_HOST_MARK in url and "openapivts" not in url:
        raise ConfigError("실전 URL은 허용하지 않습니다(모의투자 전용 브로커)")
    if url != PAPER_BASE_URL:
        raise ConfigError(f"모의투자 URL만 허용합니다: {PAPER_BASE_URL}")
    missing = [n for n, v in (("KIS_PAPER_APP_KEY", s.app_key), ("KIS_PAPER_APP_SECRET", s.app_secret),
                              ("KIS_PAPER_ACCOUNT", s.account)) if not v]
    if missing:
        raise ConfigError(f"모의투자 설정 누락: {', '.join(missing)} (.env.paper 또는 환경변수)")
    if s.app_key in live or s.app_secret in live:
        raise ConfigError("모의투자 키가 실전 키와 같습니다 — 실전 URL·키 조합 방지를 위해 시작하지 않습니다")
    if not re.fullmatch(r"\d{8}", s.account) or not re.fullmatch(r"\d{2}", s.product_code):
        raise ConfigError("계좌번호 형식: 종합계좌 8자리-상품코드 2자리(KIS_PAPER_ACCOUNT=<8자리>-<2자리>)")


class KisBroker(Broker):
    account_kind = "paper"

    def __init__(self, settings: KisPaperSettings, live: set[str] | None = None, session=None, timeout: float = 10.0):
        validate_paper_settings(settings, live if live is not None else live_secrets())
        self._s = settings
        self._redactor = Redactor([settings.app_key, settings.app_secret, settings.account])
        self._token: str | None = None
        if session is None:
            import requests
            session = requests.Session()
        self._http = session
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"KisBroker(paper, key_set={bool(self._s.app_key)}, account={mask(self._s.account)})"

    # ------------------------------------------------------------ 공통 요청

    def _fail(self, msg: object) -> BrokerError:
        return BrokerError(self._redactor.redact(msg))

    def _auth(self) -> str:
        if self._token:
            return self._token
        try:
            r = self._http.post(self._s.base_url + TOKEN_PATH, timeout=self._timeout,
                                json={"grant_type": "client_credentials", "appkey": self._s.app_key,
                                      "appsecret": self._s.app_secret})
            body = r.json()
        except Exception as e:  # 네트워크·JSON 오류 — 메시지에 URL·키가 섞일 수 있어 지운다
            raise self._fail(f"토큰 발급 실패: {e}") from None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise self._fail(f"토큰 발급 실패: HTTP {getattr(r, 'status_code', '?')}")
        self._token = token
        self._redactor.add(token)
        return token

    def _call(self, method: str, path: str, tr_id: str, payload: dict) -> dict:
        if tr_id not in PAPER_TR_IDS:
            raise self._fail(f"모의투자 tr_id가 아님: {tr_id}")
        headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self._auth()}",
                   "appkey": self._s.app_key, "appsecret": self._s.app_secret, "tr_id": tr_id, "custtype": "P"}
        url = self._s.base_url + path
        try:
            if method == "POST":
                r = self._http.post(url, headers=headers, json=payload, timeout=self._timeout)
            else:
                r = self._http.get(url, headers=headers, params=payload, timeout=self._timeout)
            body = r.json()
        except Exception as e:
            raise self._fail(f"{tr_id} 호출 실패: {e}") from None
        if not isinstance(body, dict) or body.get("rt_cd") != "0":
            msg = body.get("msg1", "") if isinstance(body, dict) else ""
            raise self._fail(f"{tr_id} 오류(HTTP {getattr(r, 'status_code', '?')}): {msg}")
        return body

    def _acct(self) -> dict:
        return {"CANO": self._s.account, "ACNT_PRDT_CD": self._s.product_code}

    # ------------------------------------------------------------ 조회

    def _balance_body(self) -> dict:
        return self._call("GET", BALANCE_PATH, TR_BALANCE, {
            **self._acct(), "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})

    def get_balance(self) -> Balance:
        out2 = self._balance_body().get("output2") or [{}]
        row = out2[0] if isinstance(out2, list) else out2
        try:
            return Balance(total_value=float(row["tot_evlu_amt"]), cash=float(row["dnca_tot_amt"]))
        except (KeyError, TypeError, ValueError):
            raise self._fail("잔고 응답에 tot_evlu_amt/dnca_tot_amt 없음") from None

    def get_positions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self._balance_body().get("output1") or []:
            try:
                q = int(float(row["hldg_qty"]))
            except (KeyError, TypeError, ValueError):
                raise self._fail("잔고 응답에 pdno/hldg_qty 없음") from None
            if q:
                out[str(row["pdno"])] = q
        return out

    # ------------------------------------------------------------ 주문(모의투자)

    def place_order(self, ticker: str, side: str, qty: int, ref_price: float) -> OrderStatus:
        if side not in ("buy", "sell") or qty <= 0:
            raise self._fail(f"잘못된 주문: {side} {qty}")
        body = self._call("POST", ORDER_CASH_PATH, TR_BUY if side == "buy" else TR_SELL, {
            **self._acct(), "PDNO": ticker, "ORD_DVSN": "01",  # 01 = 시장가
            "ORD_QTY": str(int(qty)), "ORD_UNPR": "0", "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""})
        out = body.get("output") or {}
        odno, org = out.get("ODNO"), out.get("KRX_FWDG_ORD_ORGNO")
        if not odno:
            raise self._fail("주문 응답에 ODNO 없음")
        return OrderStatus(f"{org or ''}:{odno}", "submitted", 0, None)

    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        _, odno = broker_order_id.split(":", 1)
        today = date.today().strftime("%Y%m%d")
        body = self._call("GET", DAILY_CCLD_PATH, TR_DAILY_CCLD, {
            **self._acct(), "INQR_STRT_DT": today, "INQR_END_DT": today, "SLL_BUY_DVSN_CD": "00", "PDNO": "",
            "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": odno,
            "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        rows = [r for r in (body.get("output1") or []) if str(r.get("odno", "")).lstrip("0") == odno.lstrip("0")]
        if not rows:
            return OrderStatus(broker_order_id, "submitted", 0, None, "체결 조회에 아직 없음")
        r = rows[0]
        filled, ordered = int(float(r.get("tot_ccld_qty") or 0)), int(float(r.get("ord_qty") or 0))
        avg = float(r["avg_prvs"]) if r.get("avg_prvs") not in (None, "", "0") else None
        if r.get("cncl_yn") == "Y" or int(float(r.get("cnc_cfrm_qty") or 0)) > 0:
            status = "partial" if filled else "cancelled"
        else:
            status = "filled" if ordered and filled >= ordered else ("partial" if filled else "submitted")
        return OrderStatus(broker_order_id, status, filled, avg)

    def list_orders(self, since_date: str) -> list[BrokerOrder]:
        """
        since_date~오늘 주문체결 조회(VTTC0081R, 조회 전용) — 재시작 복구용.
        TODO(모의 호출로 확인): 연속 조회(CTX_AREA_*) — 첫 페이지만 읽는다(하루 주문 몇 건 규모 전제).
        TODO(모의 호출로 확인): 잔여수량 필드 `rmn_qty` — 없으면 주문수량 − 체결수량 − 취소확인수량으로 계산한다.
        `sll_buy_dvsn_cd`: 01 매도, 02 매수(공식 샘플 기준).
        """
        start = since_date.replace("-", "")
        body = self._call("GET", DAILY_CCLD_PATH, TR_DAILY_CCLD, {
            **self._acct(), "INQR_STRT_DT": start, "INQR_END_DT": date.today().strftime("%Y%m%d"),
            "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": "00", "INQR_DVSN": "00", "INQR_DVSN_3": "00",
            "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        out = []
        for r in body.get("output1") or []:
            odno = str(r.get("odno") or "").strip()
            if not odno:
                continue
            num = lambda k: int(float(r.get(k) or 0))  # noqa: E731
            ordered, filled, cancelled = num("ord_qty"), num("tot_ccld_qty"), num("cnc_cfrm_qty")
            closed = r.get("cncl_yn") == "Y" or cancelled > 0
            open_qty = 0 if closed else (num("rmn_qty") if r.get("rmn_qty") not in (None, "")
                                         else max(0, ordered - filled - cancelled))
            if closed:
                status = "partial" if filled else "cancelled"
            else:
                status = "filled" if ordered and filled >= ordered else ("partial" if filled else "submitted")
            avg = float(r["avg_prvs"]) if r.get("avg_prvs") not in (None, "", "0") else None
            side = {"01": "sell", "02": "buy"}.get(str(r.get("sll_buy_dvsn_cd")), "unknown")
            out.append(BrokerOrder(f"{r.get('ord_gno_brno') or ''}:{odno}", str(r.get("pdno") or ""), side,
                                   ordered, filled, avg, open_qty, status))
        return out

    def cancel_order(self, broker_order_id: str) -> OrderStatus:
        org, odno = broker_order_id.split(":", 1)
        self._call("POST", ORDER_CANCEL_PATH, TR_CANCEL, {
            **self._acct(), "KRX_FWDG_ORD_ORGNO": org, "ORGN_ODNO": odno, "ORD_DVSN": "01",
            "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0", "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX"})  # 02 = 취소, 잔량 전부
        return self.get_order_status(broker_order_id)
