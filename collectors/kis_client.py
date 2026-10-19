"""
한국투자증권 KIS API 클라이언트.

주의: 이 파일에는 주문(매수/매도) 기능을 넣지 않는다.
조회 전용이다. 매매 엔드포인트는 의도적으로 구현하지 않았다.

사용 전 준비:
  1. https://apiportal.koreainvestment.com 에서 KIS Developers 신청
  2. 모의투자 계좌로 발급 (실계좌 키는 나중에)
  3. .env 에 KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT 입력

Rate limit: 초당 20건. _throttle()로 방어한다.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from config import settings
from db import store

TOKEN_CACHE = Path(__file__).resolve().parent.parent / ".kis_token.json"  # .gitignore에 포함돼 있음(보안 원칙 5)
BASE_REAL = "https://openapi.koreainvestment.com:9443"
BASE_PAPER = "https://openapivts.koreainvestment.com:29443"  # 모의투자

# investor_flow() 응답 필드명 — KIS Open API "국내주식 시세분석 > 주식현재가 투자자"
# 문서를 기준으로 작성했지만, 이 프로젝트는 KIS 키 없이 진행하라는 제약 때문에
# 실제 키로 한 번도 호출해보지 못했다(4단계: "키 없이 미리 구현"). 그래서
# 아래 tr_id·경로·필드명은 **전부 공식 문서 재확인 전까지는 최선의 추정**이다
# — 실제 키 발급 후 가장 먼저 raw 응답을 한 번 출력해서 이 값들이 맞는지
# 확인할 것. 틀려도 바로 죽지 않도록 _parse_investor_flow_response()는
# 필드가 없으면 값을 지어내지 않고 None으로 둔다(원칙: 없으면 '없음').
#
# 2026-10-18 대조: koreainvestment/open-trading-api(commit 2077773, 2026-10-14)의
# examples_llm/domestic_stock/inquire_investor/(inquire_investor.py·chk_inquire_investor.py)와
# 경로·tr_id·필드명이 **모두 일치**한다(실전·모의 tr_id 동일, 요청 파라미터는
# FID_COND_MRKT_DIV_CODE="J"·FID_INPUT_ISCD=종목코드). 남은 확인: 실제 키로 응답을 한 번 받아보는 것.
INVESTOR_FLOW_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"  # 공식 샘플과 일치(2026-10-18)
INVESTOR_FLOW_TR_ID = "FHKST01010900"  # 공식 샘플과 일치(2026-10-18)
_INVESTOR_FLOW_DATE_FIELD = "stck_bsop_date"  # 주식 영업 일자 YYYYMMDD — 공식 샘플과 일치
_INVESTOR_FLOW_FIELD_MAP = {
    # 내부 키(db/schema.sql investor_flow 컬럼명) -> KIS 응답 필드명(공식 샘플과 일치)
    "individual": "prsn_ntby_qty",    # 개인 순매수 수량
    "foreigner": "frgn_ntby_qty",     # 외국인 순매수 수량
    "institution": "orgn_ntby_qty",   # 기관계 순매수 수량
}
# 단위: 위 세 필드는 **수량**(주)이다. 금액이 필요하면 같은 응답의 prsn/frgn/orgn_ntby_tr_pbmn
# (순매수 거래 대금)을 쓴다 — analysis/signals.ksj_foreign_sell()은 부호와 합산만 쓰므로 어느 쪽이든 동작.


def _to_int_or_none(value) -> int | None:
    """값을 지어내지 않는다 — 없거나 숫자로 못 바꾸면 0이 아니라 None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_investor_flow_response(raw: dict, ticker: str) -> list[dict]:
    """
    KIS investor_flow 응답(raw)을 db.store.upsert_flow()가 바로 받는 형태로
    정규화한다: [{"ticker", "date"(YYYY-MM-DD), "individual", "foreigner",
    "institution"}, ...] (개인/외국인/기관 중 하나라도 응답에 없으면 그 값만
    None — 행 전체를 버리지 않는다).

    날짜 필드(_INVESTOR_FLOW_DATE_FIELD)가 없거나 8자리(YYYYMMDD)가 아닌
    행은 날짜를 지어낼 수 없으므로 건너뛴다. output이 list가 아니면(예상한
    응답 모양이 아니면) 빈 리스트를 돌려준다 — 여기서 예외를 던지지 않는
    이유는 investor_flow()가 이 함수의 결과를 그대로 db.store.upsert_flow()에
    넘기는 배치 성격이라, 응답 모양이 바뀐 걸 "데이터 0건"으로 다루고 위
    (collect_daily.py 등) 레이어가 이미 가진 실패 처리 경로를 그대로 타게
    하는 게 더 안전하다고 판단했다.
    """
    output = raw.get("output")
    if not isinstance(output, list):
        return []
    rows = []
    for item in output:
        if not isinstance(item, dict):
            continue
        raw_date = item.get(_INVESTOR_FLOW_DATE_FIELD)
        if not raw_date or len(str(raw_date)) != 8 or not str(raw_date).isdigit():
            continue
        s = str(raw_date)
        rows.append({
            "ticker": ticker,
            "date": f"{s[:4]}-{s[4:6]}-{s[6:8]}",
            **{key: _to_int_or_none(item.get(field)) for key, field in _INVESTOR_FLOW_FIELD_MAP.items()},
        })
    return rows


class KisClient:
    def __init__(self, paper: bool = True):
        self.base = BASE_PAPER if paper else BASE_REAL
        self.key = settings.kis_app_key
        self.secret = settings.kis_app_secret
        self._last_call = 0.0
        self._token: str | None = None

    def __repr__(self) -> str:
        # 실수로 print(client)·로그·예외 traceback에 키/토큰 값이 찍히지 않도록
        # (보안 원칙) 존재 여부만 보여주는 repr을 쓴다 — 기본 repr은 __dict__를
        # 그대로 노출해서 self.key/self.secret/self._token 값이 그대로 찍힌다.
        return f"KisClient(base={self.base!r}, key_set={bool(self.key)}, secret_set={bool(self.secret)})"

    # ---------- 내부 ----------

    def _throttle(self, min_interval: float = 0.06) -> None:
        """초당 20건 제한 대응. 여유를 둬서 0.06초."""
        gap = time.time() - self._last_call
        if gap < min_interval:
            time.sleep(min_interval - gap)
        self._last_call = time.time()

    def _load_cached_token(self) -> str | None:
        """토큰은 24시간 유효. 매번 발급하면 횟수 제한에 걸린다."""
        if not TOKEN_CACHE.exists():
            return None
        try:
            data = json.loads(TOKEN_CACHE.read_text())
            if datetime.fromisoformat(data["expires_at"]) > datetime.now():
                return data["token"]
        except Exception:
            pass
        return None

    def _save_token(self, token: str, hours: int = 23) -> None:
        TOKEN_CACHE.write_text(json.dumps({
            "token": token,
            "expires_at": (datetime.now() + timedelta(hours=hours)).isoformat(),
        }))

    @property
    def token(self) -> str:
        if self._token:
            return self._token
        cached = self._load_cached_token()
        if cached:
            self._token = cached
            return cached
        self._throttle()
        r = requests.post(
            f"{self.base}/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": self.key, "appsecret": self.secret},
            timeout=10,
        )
        r.raise_for_status()
        tok = r.json()["access_token"]
        self._save_token(tok)
        self._token = tok
        return tok

    def _headers(self, tr_id: str) -> dict:
        return {
            "authorization": f"Bearer {self.token}",
            "appkey": self.key,
            "appsecret": self.secret,
            "tr_id": tr_id,
            "content-type": "application/json; charset=utf-8",
        }

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        self._throttle()
        r = requests.get(f"{self.base}{path}", headers=self._headers(tr_id),
                         params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    # ---------- 조회 (구현할 부분) ----------

    def current_price(self, code: str = "000660") -> dict:
        """
        현재가 조회.
        TODO: tr_id 와 응답 파싱은 공식 문서 확인 후 채운다.
              https://apiportal.koreainvestment.com 의 [국내주식] 기본시세
        """
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        return data.get("output", {})

    def daily_prices(self, code: str = "000660", days: int = 100) -> list[dict]:
        """일봉 조회. TODO: 응답 필드명 매핑."""
        raise NotImplementedError("KIS API 키 발급 후 문서 보고 구현")

    def investor_flow(self, code: str = "000660") -> list[dict]:
        """
        투자자별(개인/외국인/기관) 순매수 조회 — 조회 전용(주문 아님, 이
        클라이언트에 주문 엔드포인트는 아예 없다 — CLAUDE.md 원칙 2).

        반환값은 db.store.upsert_flow()가 바로 받는 행 리스트
        [{"ticker","date","individual","foreigner","institution"}, ...]다.
        tr_id·엔드포인트 경로·필드명은 모두 모듈 상단 INVESTOR_FLOW_* 주석의
        TODO대로 공식 문서 재확인 전 최선의 추정이다 — 이 세션은 KIS 키 없이
        진행해서 실제로 호출해본 적이 없다. 실제 키로 처음 호출할 때는 raw
        응답을 한 번 그대로 로그로 남겨서(키/토큰 값은 절대 포함하지 않고
        응답 바디만) 필드명이 맞는지 직접 확인할 것.
        """
        raw = self._get(
            INVESTOR_FLOW_PATH,
            tr_id=INVESTOR_FLOW_TR_ID,
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        return parse_investor_flow_response(raw, code)


def collect_investor_flow(code: str = "000660", paper: bool = True) -> int:
    """
    investor_flow()를 호출해 db.store.upsert_flow()로 저장하는 얇은 배선
    (다른 collectors/*.py의 collect_all() 패턴과 동일). KIS 키가 없으면
    settings.kis_ready가 False라 **네트워크를 아예 시도하지 않고** 0을
    반환한다 — collect_daily.py에 이 job을 추가할 때 KIS 키 없는 환경에서도
    조용히 건너뛸 수 있게 하기 위해서다(이번 4단계에서는 아직 collect_daily.py
    의 job으로 묶지 않았다 — 실제 키로 한 번도 검증 못 한 코드를 매일 배치에
    자동으로 태우는 건 더 신중해야 한다고 판단, CLAUDE.md "아직 안 한 것" 참고).
    """
    if not settings.kis_ready:
        return 0
    store.init_db()
    rows = KisClient(paper=paper).investor_flow(code)
    return store.upsert_flow(rows)


if __name__ == "__main__":
    c = KisClient(paper=True)
    print(c.current_price("000660"))
