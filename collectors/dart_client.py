"""
OpenDART(금융감독원 전자공시시스템 Open API) 클라이언트.

조회 전용 API만 존재한다 — 매매 관련 기능 자체가 DART API에 없으므로
CLAUDE.md 원칙 2(매매 기능 미구현)와는 애초에 무관하다.

사용 전 준비:
  1. https://opendart.fss.or.kr 에서 API 키 발급 (무료, 가입 즉시 발급)
  2. .env 에 DART_API_KEY 입력

**이 세션은 DART 키 없이 구현했다(사용자 요청: "실제 호출 없이 구현하고
키만 넣으면 동작하는 상태로").** 아래 엔드포인트 경로·파라미터·응답
필드명은 OpenDART 공식 문서(https://opendart.fss.or.kr/guide/main.do)
기준 best-effort로 작성했고, 실제 키로 한 번도 호출해보지 못해 확신이
낮은 부분은 전부 TODO로 표시해뒀다 — 지어내지 않았다. 키 발급 후 가장
먼저 raw 응답을 한 번 그대로 출력해서(키 값은 절대 출력하지 않고 응답
바디만) 이 TODO들을 확인할 것.

**일일 호출 한도**: 공식 문서상 계정별 일일 호출 한도가 있다(정확한 숫자는
키 발급 화면에서 확인 — 이 세션엔 키가 없어 재확인 못함, TODO). 이
클라이언트는 그 한도를 안전하게 지키기 위해 (1) 호출 간 최소 간격
(_throttle)과 (2) 이미 DB에 저장된 보고서/공시는 다시 받지 않는 로컬
캐시(아래 collect_financials의 financial_report_exists 체크, corp_code_map
의 로컬 JSON 캐시)를 둔다. 재무제표 보고서는 한 번 공시되면 바뀌지 않는다고
보고(정정공시가 있을 수 있으나 이 세션 범위 밖), "이미 있으면 다시 안 받는다"
가 안전하다.

**보안**: 키는 .env의 DART_API_KEY에서만 읽는다. DART API는 KIS와 달리
인증을 쿠키/헤더가 아니라 **쿼리 파라미터**(crtfc_key)로 받기 때문에,
요청이 실패하면 requests 예외 메시지에 요청 URL이 그대로 포함되고 거기에
키 값이 들어 있을 수 있다 — 그래서 모든 예외 메시지는 `_mask()`로 키
문자열을 치환한 뒤에만 다시 던지거나 출력한다(로그·예외·테스트 출력
전부). `__repr__`도 키 존재 여부만 보여준다.
"""

from __future__ import annotations

import json
import time
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import requests

from config import settings
from db import store

BASE = "https://opendart.fss.or.kr/api"
CORP_CODE_PATH = "corpCode.xml"              # 고유번호 전체 다운로드(zip) — TODO: 공식문서 재확인
SINGLE_ACNT_PATH = "fnlttSinglAcnt.json"      # 단일회사 주요계정 — TODO: 공식문서 재확인
SINGLE_ACNT_ALL_PATH = "fnlttSinglAcntAll.json"  # 단일회사 전체 재무제표 — TODO: 공식문서 재확인
DISCLOSURE_LIST_PATH = "list.json"            # 공시검색 — TODO: 공식문서 재확인
STOCK_TOTAL_PATH = "stockTotqySttus.json"     # 주식의 총수 현황(발행주식수) — TODO: 공식문서 재확인, 신뢰도 낮음
TREASURY_DECISION_PATH = "tsstkAqDecsn.json"  # 자기주식 취득 결정(주요사항보고서) — TODO: 공식문서 재확인, 신뢰도 낮음.
                                              # 응답의 취득 예정 기간 필드명은 analysis/events.py의 BUYBACK_PERIOD_*_FIELDS(후보, TODO)

# 보고서 코드 — 일반적으로 알려진 값(TODO: 공식문서로 재확인).
REPORT_CODE_Q1 = "11013"
REPORT_CODE_HALF = "11012"
REPORT_CODE_Q3 = "11014"
REPORT_CODE_ANNUAL = "11011"
# 한 사업연도 안에서의 "누적" 순서 — 분기 단독값 역산(섹션 3)에 쓴다.
REPORT_CODE_CUMULATIVE_ORDER = [REPORT_CODE_Q1, REPORT_CODE_HALF, REPORT_CODE_Q3, REPORT_CODE_ANNUAL]

DART_CORP_CODE_CACHE = Path(__file__).resolve().parent.parent / ".dart_corp_code_cache.json"  # .gitignore에 포함

# DART 응답 공통 래퍼의 성공 코드. "013"은 "조회된 데이터가 없습니다"로
# 비교적 널리 알려져 있어 에러가 아니라 빈 리스트로 처리한다(TODO: 나머지
# 상태코드 의미는 공식문서 재확인 — 여기서는 000/013 외엔 전부 에러로 다룬다).
STATUS_OK = "000"
STATUS_NO_DATA = "013"


class DartApiError(Exception):
    """DART 응답이 실패(status != 000/013)거나 네트워크 요청 자체가 실패했을 때.
    메시지는 항상 _mask()를 거친 뒤에만 여기 담긴다(키 노출 방지)."""


def _to_float_or_none(value) -> float | None:
    """값을 지어내지 않는다 — 없거나 숫자로 못 바꾸면 0이 아니라 None.
    DART 금액 필드는 보통 쉼표 포함 문자열(예: "1,234,567")로 온다고 알려져
    있어 쉼표를 제거하고 변환한다(TODO: 공식문서 재확인)."""
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _rcept_dt_from_rcept_no(rcept_no: str | None) -> str | None:
    """
    rcept_no(접수번호)의 앞 8자리가 접수일(YYYYMMDD)이라는 건 비교적 널리
    알려진 DART 관행이다(TODO: 공식문서로 확정 재확인 — 추정임을 명시).
    응답에 rcept_dt 필드가 직접 없을 때의 **대체 수단**으로만 쓴다. 8자리
    숫자가 아니면 지어내지 않고 None.
    """
    if not rcept_no or len(str(rcept_no)) < 8 or not str(rcept_no)[:8].isdigit():
        return None
    s = str(rcept_no)[:8]
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _check_status(data: dict) -> list[dict]:
    """DART 공통 응답 래퍼({"status","message","list"})를 검사한다.
    status가 000이면 list(없으면 빈 리스트), 013(데이터 없음)이면 빈 리스트,
    그 외는 DartApiError(메시지에 공식 message 필드만 담음 — 키 노출 경로 아님)."""
    status = data.get("status")
    if status == STATUS_OK:
        lst = data.get("list", [])
        return lst if isinstance(lst, list) else []
    if status == STATUS_NO_DATA:
        return []
    raise DartApiError(f"DART API 오류 status={status} message={data.get('message')}")


def parse_corp_code_xml(xml_bytes: bytes) -> list[dict]:
    """
    corpCode.xml(zip 안의 XML)을 파싱한다. 태그 이름(corp_code/corp_name/
    stock_code/modify_date)은 TODO: 공식문서 재확인 — 비교적 널리 알려진
    이름을 썼다. 태그가 없으면 해당 필드만 None(지어내지 않음). stock_code가
    공백/빈 값이면(비상장사) 그 종목은 상장사 매핑에 의미가 없으므로 결과에
    그대로 두되(값 None), 호출부(corp_code_map)가 필터링한다.
    """
    root = ElementTree.fromstring(xml_bytes)
    rows = []
    for node in root.findall("list"):
        def _text(tag: str) -> str | None:
            el = node.find(tag)
            if el is None or el.text is None:
                return None
            t = el.text.strip()
            return t or None

        rows.append({
            "corp_code": _text("corp_code"),
            "corp_name": _text("corp_name"),
            "stock_code": _text("stock_code"),
            "modify_date": _text("modify_date"),
        })
    return rows


def normalize_financial_rows(
    raw_list: list[dict], corp_code: str, bsns_year: str, reprt_code: str,
) -> list[dict]:
    """
    DART 재무계정 응답(raw_list)을 db.store.upsert_financials()가 받는 행
    모양으로 바꾼다. 필드명 후보(TODO: 공식문서 재확인, 비교적 널리 알려진
    이름들):
      account_nm(계정명), fs_div(연결/별도), sj_div(재무제표구분),
      thstrm_amount(당기금액), stock_code, rcept_no(접수번호).

    rcept_dt(접수일)는 응답에 직접 있으면 그걸 쓰고, 없으면 rcept_no 앞
    8자리로 추정한다(_rcept_dt_from_rcept_no). **둘 다 없으면 그 행은
    버린다** — rcept_dt는 미래참조 방지의 핵심이라 이 값이 없는 재무
    데이터는 날짜를 지어내서 저장하지 않는다(원칙).
    """
    rows = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        account_nm = item.get("account_nm")
        if not account_nm:
            continue
        rcept_no = item.get("rcept_no")
        rcept_dt = item.get("rcept_dt") or _rcept_dt_from_rcept_no(rcept_no)
        if not rcept_dt:
            continue
        rows.append({
            "corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code,
            "account_nm": account_nm, "fs_div": item.get("fs_div"), "sj_div": item.get("sj_div"),
            "thstrm_amount": _to_float_or_none(item.get("thstrm_amount")),
            "stock_code": item.get("stock_code"), "rcept_no": rcept_no, "rcept_dt": rcept_dt,
        })
    return rows


# 발행주식수 필드명 — **신뢰도가 가장 낮은 부분**이다. 여러 후보를 순서대로
# 시도하고 전부 실패하면 None(지어내지 않음). TODO: 공식문서로 전부 재확인.
_SHARES_FIELD_CANDIDATES = ["istc_totqy", "now_to_isu_stock_totqy", "distb_stock_co"]
_SHARES_SE_TOTAL_VALUE = "합계"  # "구분"(se) 필드가 있으면 "합계" 행을 우선 — TODO: 공식문서 재확인
SHARES_OUTSTANDING_ACCOUNT_NM = "발행주식총수"  # 정식 재무제표 계정은 아니지만 financials 테이블의
# as-of 조회 패턴을 재사용하려고 합성 계정명으로 저장한다(analysis/dart_financials.py 참고).


def extract_shares_outstanding(raw_list: list[dict]) -> float | None:
    """
    주식총수 현황(stock_total_count) 응답에서 발행주식총수를 뽑는다.
    "합계" 구분(se) 행이 있으면 그 행을 우선 쓰고, 없으면 첫 행을 쓴다
    (추정 — 보통주만 있는 경우가 많아서). 후보 필드명을 순서대로 시도하고
    전부 없거나 숫자로 못 바꾸면 None — 이 경우 PBR 계산은 "발행주식수
    확인 불가"로 건너뛴다(지어내지 않음).
    """
    rows = [r for r in raw_list if isinstance(r, dict)]
    if not rows:
        return None
    row = next((r for r in rows if r.get("se") == _SHARES_SE_TOTAL_VALUE), rows[0])
    for field in _SHARES_FIELD_CANDIDATES:
        if field in row:
            val = _to_float_or_none(row[field])
            if val is not None:
                return val
    return None


def normalize_disclosure_rows(raw_list: list[dict]) -> list[dict]:
    """
    공시검색(list.json) 응답을 db.store.upsert_disclosures()가 받는 행
    모양으로 바꾼다. 필드명 후보(TODO: 공식문서 재확인):
      rcept_no, corp_code, corp_name, stock_code, report_nm, rcept_dt,
      flr_nm(제출인), rm(비고).

    rcept_no가 없으면 그 행의 유일키를 만들 수 없으므로 버린다. rcept_dt가
    응답에 없으면 rcept_no로 추정하되, 그마저 안 되면 버린다(지어내지 않음).
    """
    rows = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        rcept_no = item.get("rcept_no")
        if not rcept_no:
            continue
        rcept_dt = item.get("rcept_dt") or _rcept_dt_from_rcept_no(rcept_no)
        if not rcept_dt:
            continue
        rows.append({
            "rcept_no": rcept_no, "corp_code": item.get("corp_code"),
            "corp_name": item.get("corp_name"), "stock_code": item.get("stock_code"),
            "report_nm": item.get("report_nm"), "rcept_dt": rcept_dt,
            "flr_nm": item.get("flr_nm"), "rm": item.get("rm"),
        })
    return rows


class DartClient:
    def __init__(self):
        self.key = settings.dart_api_key
        self._last_call = 0.0

    def __repr__(self) -> str:
        # KIS 클라이언트와 같은 이유로(보안 원칙) 키 값을 절대 노출하지 않는다.
        return f"DartClient(key_set={bool(self.key)})"

    def _mask(self, text: str) -> str:
        """예외 메시지에 키 값이 그대로 들어가는 걸 막는다 — DART는 인증이
        쿼리 파라미터(crtfc_key)라서 요청 실패 시 requests 예외 메시지에
        요청 URL이 그대로 포함되고 거기에 키가 들어 있다."""
        if self.key and self.key in text:
            text = text.replace(self.key, "***")
        return text

    def _throttle(self, min_interval: float = 0.1) -> None:
        """호출 간 최소 간격. 정확한 분당/일일 한도는 TODO(공식문서 재확인) —
        보수적으로 짧은 간격만 둔다. 일일 호출 한도 자체는 이 클라이언트가
        계수하지 않고, collect_financials()의 로컬 캐시(이미 받은 보고서는
        재호출 안 함)로 호출 수를 줄이는 쪽으로 대응한다."""
        gap = time.time() - self._last_call
        if gap < min_interval:
            time.sleep(min_interval - gap)
        self._last_call = time.time()

    def _get_json(self, path: str, params: dict) -> dict:
        self._throttle()
        try:
            r = requests.get(f"{BASE}/{path}", params={**params, "crtfc_key": self.key}, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            raise DartApiError(self._mask(str(e))) from None

    def _get_bytes(self, path: str, params: dict) -> bytes:
        self._throttle()
        try:
            r = requests.get(f"{BASE}/{path}", params={**params, "crtfc_key": self.key}, timeout=30)
            r.raise_for_status()
            return r.content
        except requests.exceptions.RequestException as e:
            raise DartApiError(self._mask(str(e))) from None

    # ---------- 조회 ----------

    def corp_code_map(self, stock_codes: list[str] | None = None, use_cache: bool = True) -> dict[str, str]:
        """
        고유번호(corp_code) 전체 목록을 받아 {stock_code: corp_code} 딕셔너리로
        돌려준다(비상장사는 stock_code가 없어 제외). 전체 목록은 수만 건이라
        매번 다시 받지 않고 DART_CORP_CODE_CACHE(로컬 JSON, .gitignore 포함)에
        캐싱한다 — 코드 자체가 바뀌는 일은 드물다고 보고(TODO: 갱신 주기는
        공식문서 재확인), use_cache=False로 강제 재다운로드 가능.

        stock_codes를 주면 그 종목코드만 걸러서 돌려준다(예: ["000660",
        "005930"]) — 요청한 종목코드가 전체 목록에 없으면 결과 딕셔너리에서
        조용히 빠진다(지어내지 않음, 호출부가 누락을 확인해야 함).
        """
        full_map = self._load_corp_code_cache() if use_cache else None
        if full_map is None:
            raw_zip = self._get_bytes(CORP_CODE_PATH, {})
            with zipfile.ZipFile(BytesIO(raw_zip)) as zf:
                xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
                if xml_name is None:
                    raise DartApiError("corpCode.xml 응답(zip) 안에 xml 파일을 찾지 못함")
                xml_bytes = zf.read(xml_name)
            rows = parse_corp_code_xml(xml_bytes)
            full_map = {r["stock_code"]: r["corp_code"] for r in rows if r.get("stock_code") and r.get("corp_code")}
            self._save_corp_code_cache(full_map)

        if stock_codes is None:
            return full_map
        return {sc: full_map[sc] for sc in stock_codes if sc in full_map}

    def _load_corp_code_cache(self) -> dict[str, str] | None:
        if not DART_CORP_CODE_CACHE.exists():
            return None
        try:
            return json.loads(DART_CORP_CODE_CACHE.read_text())
        except Exception:
            return None

    def _save_corp_code_cache(self, full_map: dict[str, str]) -> None:
        DART_CORP_CODE_CACHE.write_text(json.dumps(full_map, ensure_ascii=False))

    def single_company_accounts(
        self, corp_code: str, bsns_year: str, reprt_code: str,
        all_accounts: bool = False, fs_div: str = "CFS",
    ) -> list[dict]:
        """
        단일회사 주요계정(all_accounts=False, 기본) 또는 전체 재무제표
        (all_accounts=True). fs_div(연결/별도)는 _all 쪽만 필수로 요구하는
        것으로 알려져 있다(TODO: 공식문서 재확인) — 주요계정 쪽은 응답에
        fs_div가 행마다 같이 온다고 보고 넘기지 않는다.
        """
        path = SINGLE_ACNT_ALL_PATH if all_accounts else SINGLE_ACNT_PATH
        params = {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code}
        if all_accounts:
            params["fs_div"] = fs_div
        data = self._get_json(path, params)
        return _check_status(data)

    def stock_total_count(self, corp_code: str, bsns_year: str, reprt_code: str) -> list[dict]:
        """
        주식의 총수 현황(발행주식수) — **신뢰도가 가장 낮은 메서드**다.
        엔드포인트 경로·필드명 전부 TODO(공식문서 재확인). 여기서 받은 값을
        그대로 쓰기 전에 analysis/dart_financials.py의 방어적 필드 탐색을
        반드시 거친다(후보 필드명 중 하나도 안 맞으면 "발행주식수 확인
        불가"로 처리 — 지어내지 않음).
        """
        data = self._get_json(STOCK_TOTAL_PATH, {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code})
        return _check_status(data)

    def disclosure_list(
        self, corp_code: str, bgn_de: str | None = None, end_de: str | None = None,
        page_no: int = 1, page_count: int = 100,
    ) -> list[dict]:
        """공시검색. bgn_de/end_de는 YYYYMMDD(TODO: 공식문서 재확인 — 흔히
        쓰이는 포맷)."""
        params = {"corp_code": corp_code, "page_no": page_no, "page_count": page_count}
        if bgn_de:
            params["bgn_de"] = bgn_de
        if end_de:
            params["end_de"] = end_de
        data = self._get_json(DISCLOSURE_LIST_PATH, params)
        return _check_status(data)

    def treasury_stock_acquisition_decision(
        self, corp_code: str, bgn_de: str, end_de: str,
    ) -> list[dict]:
        """
        자기주식 취득 결정 상세(주요사항보고서). 응답 한 건에 취득 예정 기간(시작·종료)이 들어 있다고
        알려져 있다 — analysis/events.extract_buyback_events가 이걸 details로 받아 이벤트를 만든다.
        엔드포인트 경로·파라미터 이름·필드명 전부 TODO(공식문서 재확인). **이 세션에서는 한 번도 호출하지
        않았다**(키 없음, 사용자 지시: 실제 DART 호출 금지). 응답과 공시 접수번호(rcept_no)를 어떻게
        짝지을지(보통 응답의 rcept_no 필드)도 실제 응답으로 확인해야 한다.
        """
        data = self._get_json(TREASURY_DECISION_PATH, {"corp_code": corp_code, "bgn_de": bgn_de, "end_de": end_de})
        return _check_status(data)


# ---------- collect_daily.py가 쓰는 배선 ----------

def collect_financials(
    corp_code: str, bsns_year: str, reprt_code: str,
    all_accounts: bool = False, force: bool = False,
) -> int:
    """
    이미 DB에 (corp_code, bsns_year, reprt_code) 조합이 있으면 네트워크를
    호출하지 않고 0을 반환한다(force=True면 강제 재호출) — "같은 보고서는
    다시 받지 않는다"는 로컬 캐시. DART 키가 없으면(settings.dart_ready
    False) 네트워크를 아예 시도하지 않는다.
    """
    if not settings.dart_ready:
        return 0
    store.init_db()
    if not force and store.financial_report_exists(corp_code, bsns_year, reprt_code):
        return 0
    client = DartClient()
    raw = client.single_company_accounts(corp_code, bsns_year, reprt_code, all_accounts=all_accounts)
    rows = normalize_financial_rows(raw, corp_code, bsns_year, reprt_code)
    return store.upsert_financials(rows)


def collect_shares_outstanding(corp_code: str, bsns_year: str, reprt_code: str, force: bool = False) -> int:
    """
    발행주식총수를 financials 테이블에 합성 계정명(SHARES_OUTSTANDING_
    ACCOUNT_NM)으로 저장한다. 이미 그 (corp_code, bsns_year, reprt_code)로
    저장돼 있으면 재호출하지 않는다(force=True면 강제) — collect_financials
    와 같은 로컬 캐시 패턴. stock_total_count() 응답에서 공시일을 끝내
    못 찾으면(rcept_dt도 rcept_no도 없음) 미래참조 방지를 위해 저장하지
    않고 0을 반환한다(지어내지 않음).
    """
    if not settings.dart_ready:
        return 0
    store.init_db()
    existing = store.get_financials(corp_code, SHARES_OUTSTANDING_ACCOUNT_NM)
    already = not existing.empty and (
        (existing["bsns_year"].astype(str) == str(bsns_year)) & (existing["reprt_code"] == reprt_code)
    ).any()
    if already and not force:
        return 0

    client = DartClient()
    raw = client.stock_total_count(corp_code, bsns_year, reprt_code)
    shares = extract_shares_outstanding(raw)
    if shares is None:
        return 0

    rcept_dt = None
    for item in raw:
        if isinstance(item, dict):
            rcept_dt = item.get("rcept_dt") or _rcept_dt_from_rcept_no(item.get("rcept_no"))
            if rcept_dt:
                break
    if rcept_dt is None:
        return 0

    row = {
        "corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code,
        "account_nm": SHARES_OUTSTANDING_ACCOUNT_NM, "thstrm_amount": shares, "rcept_dt": rcept_dt,
    }
    return store.upsert_financials([row])


def collect_disclosures(corp_code: str, bgn_de: str | None = None, end_de: str | None = None) -> int:
    """공시목록은 새 공시가 계속 생기므로 "이미 있으면 재호출 안 함" 캐시를
    두지 않는다 — 호출은 항상 하되, 저장은 upsert_disclosures()가 rcept_no
    로 멱등 처리한다."""
    if not settings.dart_ready:
        return 0
    store.init_db()
    client = DartClient()
    raw = client.disclosure_list(corp_code, bgn_de=bgn_de, end_de=end_de)
    rows = normalize_disclosure_rows(raw)
    return store.upsert_disclosures(rows)


# 이 프로젝트가 감시하는 KRX 개별종목(scripts/verify_outlier.py의
# KRX_EQUITY_TICKERS와 동일한 목록 — 둘을 하나로 합치지 않은 이유는 두
# 모듈이 서로 import하지 않는 기존 관행을 그대로 따르기 위해서다).
DART_WATCH_STOCK_CODES = ["000660", "005930"]


def collect_all(bsns_years: list[str] | None = None) -> int:
    """
    collect_daily.py --only dart가 부르는 진입점. 감시 종목(000660·005930)의
    corp_code를 찾고, bsns_years(기본: 올해·작년)의 4개 보고서(Q1/반기/3분기/
    사업보고서)를 각각 collect_financials()로 받고(이미 있으면 건너뜀), 최근
    1년치 공시목록도 받는다. DART 키가 없으면 네트워크를 전혀 시도하지 않고
    0을 반환한다.
    """
    if not settings.dart_ready:
        return 0
    store.init_db()
    year = datetime.now().year
    bsns_years = bsns_years if bsns_years is not None else [str(year), str(year - 1)]

    client = DartClient()
    corp_codes = client.corp_code_map(DART_WATCH_STOCK_CODES)

    total = 0
    for stock_code, corp_code in corp_codes.items():
        for y in bsns_years:
            for reprt_code in REPORT_CODE_CUMULATIVE_ORDER:
                total += collect_financials(corp_code, y, reprt_code)
                total += collect_shares_outstanding(corp_code, y, reprt_code)
        bgn_de = f"{year - 1}0101"
        end_de = datetime.now().strftime("%Y%m%d")
        total += collect_disclosures(corp_code, bgn_de=bgn_de, end_de=end_de)
    return total


if __name__ == "__main__":
    print(f"[dart_client] dart_ready={settings.dart_ready}")
    print(f"완료: {collect_all()} rows")
