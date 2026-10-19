"""
대시보드 렌더링 테스트 — FastAPI TestClient로 실제 템플릿까지 렌더링한다.

tests/test_web.py는 build_dashboard_context()의 데이터 구조만 본다(화면은 안 봄). 이 파일은 그 반대로
**HTML이 실제로 만들어지는지**를 본다:
  1. 모든 GET 페이지가 200 — 라우트를 앱에서 동적으로 모아서 돌린다(새 페이지가 생기면 자동 포함).
  2. 데이터 유무별 상태(빈 DB·시세 1~3행·40행·풍부한 데이터)에서 크래시 없이 렌더링, 빈 상태 문구 노출.
  3. 주요 카드가 존재(풍부한 데이터).
  4. 화면에 "nan"/"None"/"inf" 같은 내부 값이 새지 않는다. 검사기가 아무것도 못 잡는 둔한 함수가
     아님을 자체 테스트와 음성 대조(실제 NaN을 주입하면 잡히는지)로 확인한다.
  5. 이 앱에는 읽기(GET/HEAD) 외의 엔드포인트가 없다(원칙 2: 매매 경로 없음).
"""

from __future__ import annotations

import html as htmllib
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from analysis import events as ev  # noqa: E402
from db import store  # noqa: E402
from web import main as web_main  # noqa: E402
from web.main import HYNIX, app  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import _fake_prices, _seed_full_dataset  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


@pytest.fixture()
def client(tmp_db):
    return TestClient(app)


# ---------------------------------------------------------------- 누수 검사기

_LEAK_TOKENS = r"nan|NaN|NAN|None|inf|-inf|Infinity|null|undefined"
_VISIBLE_LEAK = re.compile(rf"(?<![\w가-힣.\-])({_LEAK_TOKENS})(?![\w가-힣\-])")
_ATTR_LEAK = re.compile(rf'="\s*({_LEAK_TOKENS})\s*"')


def visible_text(page: str) -> str:
    """style·script·주석을 걷어내고 태그를 지운 화면 글자."""
    page = re.sub(r"<(style|script)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", page))


def find_leaks(page: str) -> list[str]:
    """화면 글자와 태그 속성값에서 nan/None/inf 류 토큰을 찾는다. 비어 있으면 깨끗한 것."""
    leaks = _VISIBLE_LEAK.findall(visible_text(page))
    no_script = re.sub(r"<(style|script)\b.*?</\1>", " ", page, flags=re.S | re.I)
    leaks += [f'attr:{m}' for m in _ATTR_LEAK.findall(no_script)]
    return leaks


class TestLeakDetectorItself:
    @pytest.mark.parametrize("snippet", [
        "<td>nan%</td>", "<td>+nan%</td>", "<td>None</td>", "<span>값: inf</span>", "<td>-inf</td>",
        "<td>NaN</td>", "<td>(NaN 또는 None)</td>", '<a href="None">x</a>', '<div class="nan">x</div>',
        "<td>Infinity</td>", "<td>null</td>",
    ])
    def test_detects_leaks(self, snippet):
        assert find_leaks(snippet), snippet

    @pytest.mark.parametrize("snippet", [
        "<td>데이터 없음</td>", "<td>+3.20%</td>", "<style>.x{display:none}</style><p>ok</p>",
        "<script>var a = null; var b = None_;</script><p>ok</p>", "<p>nano finance Information</p>",
        "<!-- None --><p>ok</p>", "<td>python scripts/add_event.py</td>", "<p>Nancy · Finance · Info</p>",
    ])
    def test_does_not_flag_clean_pages(self, snippet):
        assert find_leaks(snippet) == [], snippet


# ---------------------------------------------------------------- 데이터 구성

def _seed_sparse(tmp_db, days: int):
    store.upsert_prices(_fake_prices(HYNIX, days=days, seed=1, base=300000), path=tmp_db)


def _seed_everything(tmp_db):
    """풍부한 시세 + 뉴스(출처 NULL·소문 포함) + claim(NULL 칸 포함) + 신호 + 이벤트 + 수급 + 공시 + 에이전트 분석."""
    _seed_full_dataset(tmp_db)
    prices = store.get_prices(HYNIX, path=tmp_db)
    last_day = prices.index[-1].date()
    shock_day = prices.index[-30].date()

    store.upsert_news([
        {"ticker": HYNIX, "headline": "출처 없는 헤드라인", "source": None, "url": None,
         "published_at": shock_day.isoformat(), "confidence": "news"},
        {"ticker": HYNIX, "headline": "미확인 소문", "source": "누군가", "url": "http://x.test/a",
         "published_at": shock_day.isoformat(), "confidence": "rumor"},
    ], path=tmp_db)
    store.insert_claim({
        "said_on": shock_day.isoformat(), "who": "누군가", "ticker": HYNIX, "summary": "요약 한 줄",
        "confidence": "rumor",
    }, path=tmp_db)  # price_then·target_price·affiliation·stance 전부 NULL
    store.upsert_disclosures([{
        "rcept_no": "20260101000001", "corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": HYNIX,
        "report_nm": "주요사항보고서(자기주식취득결정)", "rcept_dt": shock_day.isoformat(), "flr_nm": "SK하이닉스", "rm": None,
    }], path=tmp_db)
    store.upsert_flow([
        {"ticker": HYNIX, "date": d.date().isoformat(), "individual": 100, "foreigner": -50, "institution": None}
        for d in prices.index[-60:]
    ], path=tmp_db)

    # 채점된 claim(적중/실패) · 목표가 있는 claim · 소속·입장 라벨 — claim 스코어보드/애널리스트 카드의 NULL·NaN 경로
    store.insert_claim({"said_on": (shock_day - timedelta(days=60)).isoformat(), "who": "분석가A", "ticker": HYNIX,
                        "summary": "목표가 제시", "price_then": 300000, "target_price": 350000, "horizon_days": 30,
                        "confidence": "news", "affiliation": "어느 증권사", "stance": "매수"}, path=tmp_db)
    store.insert_claim({"said_on": (shock_day - timedelta(days=50)).isoformat(), "who": "분석가B", "ticker": HYNIX,
                        "summary": "다른 의견", "price_then": 310000, "confidence": "news"}, path=tmp_db)
    graded = store.get_claims_for_grading(path=tmp_db)
    store.grade_claim(int(graded.iloc[0]["id"]), shock_day.isoformat(), 340000.0, 1, path=tmp_db)

    # 에이전트 분석 캐시(경고 포함) · 수동 입력(최신성 배지 경로)
    store.upsert_agent_analysis({
        "ticker": HYNIX, "run_date": last_day.isoformat(), "model": "test-model", "narrative": "서술 한 줄",
        "tool_calls": "[]", "warnings": '["금지 문구 의심: 테스트"]', "created_at": last_day.isoformat(),
    }, path=tmp_db)
    store.insert_manual_input({"input_type": "rate_stage", "effective_on": (last_day - timedelta(days=200)).isoformat(),
                               "value_text": "동결"}, path=tmp_db)
    store.insert_manual_input({"input_type": "pbr", "effective_on": last_day.isoformat(), "value_num": 1.5}, path=tmp_db)

    store.upsert_expert({"expert_id": "E1", "name": "전문가1", "title": None}, path=tmp_db)
    for sid, direction in (("SIG_RISK", "risk"), ("SIG_OPP", "opportunity")):
        store.upsert_signal({"signal_id": sid, "name": sid, "expert_id": "E1", "kind": "regime_filter",
                             "direction": direction, "description": "d"}, path=tmp_db)
        store.upsert_signal_values(
            [{"date": d.date().isoformat(), "signal_id": sid, "value": None, "is_on": int(i % 7 == 0 or i >= len(prices) - 2)}
             for i, d in enumerate(prices.index)], path=tmp_db)

    store.upsert_event(ev.validate_event({"ticker": HYNIX, "event_type": "buyback",
                                          "start_date": (last_day - timedelta(days=200)).isoformat(),
                                          "end_date": (last_day - timedelta(days=120)).isoformat()}), path=tmp_db)
    store.upsert_event(ev.validate_event({"ticker": HYNIX, "event_type": "earnings",
                                          "start_date": (date.today() + timedelta(days=9)).isoformat(),
                                          "title": None, "note": None}), path=tmp_db)
    store.upsert_event(ev.validate_event({"ticker": HYNIX, "event_type": "buyback",
                                          "start_date": (date.today() - timedelta(days=3)).isoformat(),
                                          "end_date": (date.today() + timedelta(days=30)).isoformat()}), path=tmp_db)


# ---------------------------------------------------------------- 1. 모든 페이지 200

def _get_routes() -> list[str]:
    paths = []
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if "GET" in methods and "{" not in path:
            paths.append(path)
    return sorted(set(paths))


class TestEveryPageReturns200:
    def test_known_pages_are_registered(self):
        routes = _get_routes()
        assert "/" in routes and "/healthz" in routes

    @pytest.mark.parametrize("state", ["empty", "sparse", "full"])
    def test_all_get_routes_200(self, tmp_db, state):
        if state == "sparse":
            _seed_sparse(tmp_db, 3)
        elif state == "full":
            _seed_everything(tmp_db)
        client = TestClient(app)
        for path in _get_routes():
            r = client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code} ({state})"

    def test_dashboard_is_html_and_healthz_has_no_market_data(self, client):
        r = client.get("/")
        assert r.headers["content-type"].startswith("text/html")
        assert "<html" in r.text and "</html>" in r.text
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_no_state_changing_endpoints(self):
        """원칙 2: 매매는 물론 어떤 쓰기 엔드포인트도 없다 — 모든 라우트가 GET/HEAD뿐."""
        for r in app.routes:
            methods = getattr(r, "methods", None)
            if methods:
                assert methods <= {"GET", "HEAD"}, (r.path, methods)


# ---------------------------------------------------------------- 2·3. 상태별 렌더링 / 주요 카드

CORE_CARDS = [
    "전문가 경험칙 신호 · 위험 점수", "000660 vs ADR(SKHY)", "미국 반도체 관련주", "상관관계 · 베타",
    "최근 급변일", "기술적 지표 규칙 백테스트", "매수·목표·손절 기준가", "최근 뉴스", "투자 프레임워크 렌즈",
    "에이전트 리서치 요약", "전문가 발언 채점 현황", "애널리스트/의견 추적", "시장 심리 게이지",
    "오늘의 캘린더", "다가오는 45일 일정", "주시할 이벤트", "매수 전 체크", "최근 수집 로그",
]


def _card_titles(page: str) -> str:
    return " ".join(re.findall(r"<h2[^>]*>(.*?)</h2>", page, flags=re.S))


class TestCards:
    def test_all_core_cards_exist_with_full_data(self, tmp_db, client):
        _seed_everything(tmp_db)
        titles = _card_titles(client.get("/").text)
        missing = [c for c in CORE_CARDS if c not in titles]
        assert not missing, f"렌더링되지 않은 카드: {missing}"

    def test_seeded_optional_paths_really_reach_the_page(self, tmp_db, client):
        """누수 검사가 '아무것도 안 그려진 카드'를 검사하는 빈 검사가 아님을 보장한다."""
        _seed_everything(tmp_db)
        text = visible_text(client.get("/").text)
        for needle in ("서술 한 줄", "분석가A", "분석가B", "미확인 소문", "출처 없는 헤드라인", "전문가1", "SIG_RISK"):
            assert needle in text, f"시드한 '{needle}'가 화면에 없음"
        assert "미확인" in text                       # rumor 라벨

    def test_risk_and_opportunity_scores_shown_side_by_side(self, tmp_db, client):
        """2026-10-18: 기회 신호는 위험 점수를 깎지 않고 별도 지표로 나란히 표시된다."""
        _seed_everything(tmp_db)  # 마지막 날 SIG_RISK·SIG_OPP 둘 다 켜짐
        text = visible_text(client.get("/").text)
        assert "기회 지표" in text and "기회 지표 분해" in text and "위험 점수 분해" in text
        assert "상태는 위험 점수 60/80점으로 정합니다" in text  # 적중률 가중치 방식(옵션·참고)
        # 2026-10-19: 오늘의 상태는 그룹 방식. 합성 데이터는 기준 기간(2005~2018)이 없어 계산 불가 → "데이터 없음"으로 정직하게
        assert "적중률 가중치 방식(옵션" in text and "그룹 방식 상태를 계산할 수 없습니다" in text

    def test_score_ceiling_warning_lists_missing_manual_signal(self, tmp_db, client):
        _seed_everything(tmp_db)
        store.upsert_signal({"signal_id": "KHJ_RATE_STAGE", "name": "금리 단계", "expert_id": "E1", "kind": "regime_filter",
                             "direction": "risk", "description": "d", "is_manual": 1}, path=tmp_db)
        text = visible_text(client.get("/").text)
        assert "현재 최대 가능 점수" in text and "KHJ_RATE_STAGE(수동 입력 없음)" in text
        assert "3단계 판정 불가" in text or "·3단계 판정 불가" in text

    def test_move_card_shows_framing_sample_sizes_and_missing_data(self, tmp_db, client):
        _seed_everything(tmp_db)
        text = visible_text(client.get("/").text)
        assert "같은 날 함께 움직인 지표" in text and "인과 단정 아님" in text
        assert "때문에" not in text.split("최근 급변일")[1].split("기술적 지표 규칙 백테스트")[0]
        assert "베타 추정: 직전 60거래일" in text          # 추정 기간 표기
        assert "설명되지 않는 잔차" in text
        assert "투자자별 수급" in text

    def test_events_card_shows_registered_and_study_with_sample_warning(self, tmp_db, client):
        _seed_everything(tmp_db)
        text = visible_text(client.get("/").text)
        part = text.split("주시할 이벤트")[1].split("매수 전 체크")[0]
        assert "등록된 수급 이벤트" in part and "진행 중" in part and "D-9" in part
        assert "자사주 매입 종료 전후" in part and "최대손실" in part
        assert re.search(r"표본 \d+<30", part)             # 원칙 3: n<30 경고

    def test_empty_db_renders_empty_state_message(self, client):
        r = client.get("/")
        text = visible_text(r.text)
        assert r.status_code == 200
        assert "데이터 없음" in text and "kr_market.py" in text
        assert find_leaks(r.text) == []

    @pytest.mark.parametrize("days", [1, 2, 3, 40, 120])
    def test_sparse_prices_render_with_empty_state_phrases(self, tmp_db, client, days):
        _seed_sparse(tmp_db, days)
        r = client.get("/")
        assert r.status_code == 200
        text = visible_text(r.text)
        assert "데이터 없음" in text                       # 없는 부분은 지어내지 않고 이렇게 표시
        assert "아직 수집된 뉴스가 없습니다" in text
        assert "최근 급변일" in _card_titles(r.text)

    def test_missing_indicators_are_labeled_not_invented(self, tmp_db, client):
        # 000660만 있고 SOX·NVDA·VIX·코스피 등 주변 지표가 전혀 없을 때 급변일 카드
        rs = np.random.RandomState(3)
        idx = pd.bdate_range("2025-01-01", periods=200)
        close = 100000 * np.cumprod(1 + rs.normal(0, 0.02, 200))
        store.upsert_prices([{"ticker": HYNIX, "date": d.date().isoformat(), "open": c, "high": c, "low": c,
                              "close": c, "volume": 1, "source": "t"} for d, c in zip(idx, close)], path=tmp_db)
        text = visible_text(client.get("/").text)
        part = text.split("최근 급변일")[1].split("기술적 지표 규칙 백테스트")[0]
        assert "시세 데이터 없음" in part
        assert find_leaks(client.get("/").text) == []


# ---------------------------------------------------------------- 4. nan / None / inf 누수 검사

class TestNoInternalValuesLeak:
    @pytest.mark.parametrize("state", ["empty", "1", "2", "3", "40", "120", "full"])
    def test_no_nan_none_inf_anywhere(self, tmp_db, state):
        if state == "full":
            _seed_everything(tmp_db)
        elif state != "empty":
            _seed_sparse(tmp_db, int(state))
        page = TestClient(app).get("/").text
        assert find_leaks(page) == []

    def test_negative_control_injected_nan_is_caught(self, tmp_db, monkeypatch):
        """검사기가 실제 렌더링 결과에서도 동작하는지: NaN을 일부러 주입하면 반드시 잡혀야 한다."""
        _seed_everything(tmp_db)
        real = web_main.build_dashboard_context

        def poisoned():
            ctx = real()
            ctx["kpi"]["adr_premium_pct"] = float("nan")
            ctx["kpi"]["skhy_chg"] = float("inf")
            return ctx

        monkeypatch.setattr(web_main, "build_dashboard_context", poisoned)
        page = TestClient(app).get("/").text
        leaks = {t.lower() for t in find_leaks(page)}
        assert {"nan", "inf"} <= leaks, f"주입한 NaN/inf가 검사에 안 잡힘: {leaks}"
