"""
대시보드 컨텍스트 빌더 테스트.

web/main.py는 fastapi에 의존하지만, build_dashboard_context()는
Request 등 fastapi 타입을 전혀 받지 않는 순수 함수로 분리해뒀다.
여기서는 그 함수가 반환하는 데이터 구조만 검증한다 — 화면(HTML)은 보지 않는다.

이 파일은 fastapi가 설치돼 있어야 돈다 (importorskip으로 없으면 skip).
fastapi 없이 전체 파이프라인(진짜 build_dashboard_context + 진짜 템플릿)을
확인하려면 개발 중 만든 스텁 스크립트 방식을 참고할 것 — CLAUDE.md 참고.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fastapi = pytest.importorskip("fastapi")  # fastapi 미설치 환경에서는 이 모듈 전체를 skip

from db import store  # noqa: E402
from web.main import HYNIX, PEERS, build_dashboard_context  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """진짜 baseline.db를 건드리지 않도록 임시 DB로 바꿔치기."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _fake_prices(ticker: str, days: int = 260, seed: int = 0, base: float = 100.0) -> list[dict]:
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range("2024-01-01", periods=days)
    closes = np.cumprod(1 + rs.normal(0, 0.01, days)) * base
    return [
        {
            "ticker": ticker,
            "date": d.strftime("%Y-%m-%d"),
            "open": float(c), "high": float(c), "low": float(c), "close": float(c),
            "volume": 1000,
            "source": "test",
        }
        for d, c in zip(idx, closes)
    ]


def _seed_full_dataset(tmp_db):
    store.upsert_prices(_fake_prices(HYNIX, days=800, seed=1, base=312000), path=tmp_db)
    store.upsert_prices(_fake_prices("SKHY", days=800, seed=2, base=25), path=tmp_db)
    store.upsert_prices(_fake_prices("USDKRW", days=800, seed=3, base=1380), path=tmp_db)
    for i, name in enumerate(PEERS):
        store.upsert_prices(_fake_prices(name, days=800, seed=i + 10, base=100 * (i + 1)), path=tmp_db)
    store.upsert_prices(_fake_prices("VIX", days=300, seed=99, base=20), path=tmp_db)


class TestBuildDashboardContext:
    def test_empty_db_reports_no_data(self, tmp_db):
        ctx = build_dashboard_context()
        assert ctx["has_data"] is False
        assert ctx["correlations"] == []
        assert ctx["backtest"] is None
        # 캘린더는 데이터 유무와 무관하게 항상 채워져야 한다
        assert "upcoming_events" in ctx

    def test_populated_db_produces_correlations(self, tmp_db):
        _seed_full_dataset(tmp_db)
        ctx = build_dashboard_context()
        assert ctx["has_data"] is True
        assert len(ctx["correlations"]) == len(PEERS)
        for c in ctx["correlations"]:
            # 원칙 3: 표본 수는 항상 존재해야 한다
            assert "samples" in c and c["samples"] >= 0
            assert "reliable" in c

    def test_backtest_carries_sample_size_and_worst(self, tmp_db):
        _seed_full_dataset(tmp_db)
        ctx = build_dashboard_context()
        if ctx["backtest"] is not None:
            # 원칙 4: 최대손실(worst)이 항상 같이 있어야 한다
            assert "worst" in ctx["backtest"]
            assert "n" in ctx["backtest"]

    def test_adr_premium_uses_real_ratio(self, tmp_db):
        _seed_full_dataset(tmp_db)
        ctx = build_dashboard_context()
        assert ctx["adr"] is not None
        assert ctx["adr"].ratio == 10
        assert ctx["kpi"]["adr_premium_pct"] == ctx["adr"].premium_pct

    def test_sentiment_gauge_present_when_vix_available(self, tmp_db):
        _seed_full_dataset(tmp_db)
        ctx = build_dashboard_context()
        assert ctx["sentiment"] is not None
        assert 0 <= ctx["sentiment"].score <= 100
        assert ctx["gauge_svg"].strip().startswith("<svg")

    def test_six_framework_lenses_present(self, tmp_db):
        _seed_full_dataset(tmp_db)
        ctx = build_dashboard_context()
        assert len(ctx["lenses"]) == 6
        stub_lenses = [lens for lens in ctx["lenses"] if not lens.data_available]
        assert len(stub_lenses) == 3  # 가치·성장·밸류에이션은 데이터가 없어 스텁이어야 함

    def test_claim_scoreboard_present_regardless_of_price_data(self, tmp_db):
        # 000660 시세 없이도(has_data=False) claim_scoreboard는 항상 채워져야 한다.
        ctx = build_dashboard_context()
        assert ctx["has_data"] is False
        assert ctx["claim_scoreboard"] is not None
        assert ctx["claim_scoreboard"]["n"] == 0

    def test_claim_scoreboard_reflects_graded_claims(self, tmp_db):
        _seed_full_dataset(tmp_db)
        store.insert_claim({
            "said_on": "2026-01-01", "who": "테스트", "ticker": HYNIX,
            "summary": "요약", "price_then": 100, "target_price": 120,
            "horizon_days": 30, "confidence": "news",
        }, path=tmp_db)
        claim_id = int(store.get_claims_for_grading(path=tmp_db).iloc[0]["id"])
        store.grade_claim(claim_id, "2026-01-31", 130.0, 1, path=tmp_db)

        ctx = build_dashboard_context()
        board = ctx["claim_scoreboard"]
        assert board["n"] == 1
        assert board["hits"] == 1
        # 원칙 3: 표본 수가 30 미만이면 reliable=False가 반드시 붙어야 한다
        assert board["reliable"] is False

    def test_analyst_views_empty_when_no_claims(self, tmp_db):
        ctx = build_dashboard_context()
        assert ctx["analyst_views"] == []

    def test_analyst_views_present_regardless_of_price_data(self, tmp_db):
        # 000660 시세 없이도(has_data=False) 의견 자체는 보여줘야 한다 —
        # 다만 가격 변화(pct_change_since)는 계산할 기준 가격이 없어 None.
        store.insert_claim({
            "said_on": "2026-01-01", "who": "어느 애널리스트", "affiliation": "어느증권",
            "ticker": HYNIX, "summary": "요약", "stance": "매수",
            "price_then": 100000.0, "confidence": "news",
        }, path=tmp_db)
        ctx = build_dashboard_context()
        assert ctx["has_data"] is False
        assert len(ctx["analyst_views"]) == 1
        v = ctx["analyst_views"][0]
        assert v["who"] == "어느 애널리스트"
        assert v["affiliation"] == "어느증권"
        assert v["stance"] == "매수"
        assert v["pct_change_since"] is None  # 최신 시세가 없어서 계산 불가

    def test_analyst_views_computes_price_change_since_said_on(self, tmp_db):
        _seed_full_dataset(tmp_db)
        hynix_close = store.get_prices(HYNIX, path=tmp_db)
        latest = float(hynix_close.iloc[-1])
        store.insert_claim({
            "said_on": "2026-01-01", "who": "어느 애널리스트", "ticker": HYNIX,
            "summary": "요약", "stance": "매수", "price_then": latest / 2,
            "confidence": "news",
        }, path=tmp_db)
        ctx = build_dashboard_context()
        v = ctx["analyst_views"][0]
        assert v["latest_price"] == pytest.approx(latest)
        assert v["pct_change_since"] == pytest.approx(100.0)  # 2배가 됐으니 +100%

    def test_analyst_views_only_latest_per_person(self, tmp_db):
        store.insert_claim({
            "said_on": "2026-01-01", "who": "A", "ticker": HYNIX,
            "summary": "옛날", "stance": "매도", "confidence": "news",
        }, path=tmp_db)
        store.insert_claim({
            "said_on": "2026-06-01", "who": "A", "ticker": HYNIX,
            "summary": "최근", "stance": "매수", "confidence": "news",
        }, path=tmp_db)
        ctx = build_dashboard_context()
        assert len(ctx["analyst_views"]) == 1
        assert ctx["analyst_views"][0]["summary"] == "최근"

    def test_rule_backtest_absent_when_history_too_short(self, tmp_db):
        # HYNIX만 짧게(50일) 넣으면 has_data는 True가 되지만(종가가 있으니)
        # 규칙 백테스트는 RULE_BACKTEST_MIN_ROWS(250일) 미만이라 카드가 비어야 한다.
        store.upsert_prices(_fake_prices(HYNIX, days=50, seed=1), path=tmp_db)
        ctx = build_dashboard_context()
        assert ctx["has_data"] is True
        assert ctx["rule_backtest"] is None

    def test_rule_backtest_populated_with_mdd_shown(self, tmp_db):
        _seed_full_dataset(tmp_db)
        ctx = build_dashboard_context()
        rb = ctx["rule_backtest"]
        assert rb is not None
        assert len(rb["rows"]) == 6  # 5개 규칙 + buy_and_hold
        for row in rb["rows"]:
            # 원칙 4: 최대손실(MDD)이 항상 같이 있어야 한다
            assert "mdd_pct" in row
            assert "reliable" in row

    def test_price_rule_signals_absent_when_history_too_short(self, tmp_db):
        store.upsert_prices(_fake_prices(HYNIX, days=50, seed=1), path=tmp_db)
        ctx = build_dashboard_context()
        assert ctx["price_rule_signals"] is None

    def test_price_rule_signals_is_list_only_passing_rules_shown(self, tmp_db):
        _seed_full_dataset(tmp_db)
        ctx = build_dashboard_context()
        signals = ctx["price_rule_signals"]
        # 데이터가 충분하면 None이 아니라 리스트여야 한다(통과한 규칙이
        # 하나도 없으면 빈 리스트 — 합성 랜덤워크 데이터는 보통 그렇다).
        assert isinstance(signals, list)
        for rule in signals:
            # 원칙 3·4: 통과한 규칙이라도 표본 수·MDD가 항상 같이 있어야 한다
            assert {"n", "mdd_pct", "mean_return_pct", "oos_n", "oos_mean_return_pct"} <= rule.keys()
            assert rule["n"] >= 100  # 통과 기준 그대로
            assert rule["mean_return_pct"] > 0
            assert rule["oos_mean_return_pct"] > 0

    def test_news_nan_is_sanitized_to_none(self, tmp_db):
        _seed_full_dataset(tmp_db)
        store.upsert_news([
            {"ticker": HYNIX, "headline": "출처 없는 소문", "source": None, "url": None,
             "published_at": "2026-08-21", "confidence": "rumor"},
        ], path=tmp_db)
        ctx = build_dashboard_context()
        assert len(ctx["news"]) == 1
        assert ctx["news"][0]["source"] is None  # "nan" 문자열 회귀 방지

    def test_risk_state_has_contributions_and_warnings_keys_even_with_no_signals(self, tmp_db):
        # signals 테이블이 비어 있어도(seed_signals.py 안 돌린 상태) 키 자체는
        # 항상 있어야 한다 — 템플릿이 risk_state.contributions를 그냥 꺼내 쓴다.
        _seed_full_dataset(tmp_db)
        ctx = build_dashboard_context()
        assert ctx["risk_state"]["contributions"] == []
        assert ctx["risk_state"]["concentration_warnings"] == []

    def test_manual_input_freshness_present_and_flags_stale_value(self, tmp_db):
        _seed_full_dataset(tmp_db)
        store.insert_manual_input(
            {"input_type": "pbr", "effective_on": "2020-01-01", "value_num": 1.5}, path=tmp_db
        )
        ctx = build_dashboard_context()
        freshness = ctx["manual_input_freshness"]
        assert set(freshness) == {"rate_stage", "memory_margin", "pbr"}
        assert freshness["pbr"]["exists"] is True
        assert freshness["pbr"]["stale"] is True  # 2020년 입력, 기본 90일 기준 훨씬 지남
        assert freshness["rate_stage"]["exists"] is False  # 입력 안 함

    def test_adr_ratio_uses_config_override(self, tmp_db, monkeypatch):
        import web.main as webmain
        from types import SimpleNamespace

        _seed_full_dataset(tmp_db)
        monkeypatch.setattr(webmain, "settings", SimpleNamespace(adr_ratio=5, kis_ready=False, watch_kr="000660"))
        ctx = build_dashboard_context()
        assert ctx["adr"].ratio == 5

    def test_ksj_breadth_marked_approximate_when_on(self, tmp_db):
        _seed_full_dataset(tmp_db)
        store.upsert_expert({"expert_id": "KSJ", "name": "전문가 C"}, path=tmp_db)
        store.upsert_signal({
            "signal_id": "KSJ_BREADTH", "name": "반도체 쏠림(매수 소진)", "expert_id": "KSJ",
            "kind": "regime_filter", "direction": "risk", "description": "test",
        }, path=tmp_db)
        idx = pd.bdate_range("2024-01-01", periods=100)
        store.upsert_signal_values([
            {"date": d.strftime("%Y-%m-%d"), "signal_id": "KSJ_BREADTH", "value": 1.0, "is_on": int(i == len(idx) - 1)}
            for i, d in enumerate(idx)
        ], path=tmp_db)

        ctx = build_dashboard_context()
        row = next(r for r in ctx["risk_state"]["on_signals"] if r["signal_id"] == "KSJ_BREADTH")
        assert row["approximate"] is True

    def test_other_signals_not_marked_approximate(self, tmp_db):
        _seed_full_dataset(tmp_db)
        store.upsert_expert({"expert_id": "YJH", "name": "전문가 B"}, path=tmp_db)
        store.upsert_signal({
            "signal_id": "YJH_US10Y", "name": "미국 10년물", "expert_id": "YJH",
            "kind": "regime_filter", "direction": "risk", "description": "test",
        }, path=tmp_db)
        idx = pd.bdate_range("2024-01-01", periods=100)
        store.upsert_signal_values([
            {"date": d.strftime("%Y-%m-%d"), "signal_id": "YJH_US10Y", "value": 1.0, "is_on": int(i == len(idx) - 1)}
            for i, d in enumerate(idx)
        ], path=tmp_db)

        ctx = build_dashboard_context()
        row = next(r for r in ctx["risk_state"]["on_signals"] if r["signal_id"] == "YJH_US10Y")
        assert row["approximate"] is False

    def test_concentration_warning_shown_when_correlated_signals_both_on(self, tmp_db):
        _seed_full_dataset(tmp_db)
        store.upsert_expert({"expert_id": "YJH", "name": "전문가 B"}, path=tmp_db)
        store.upsert_expert({"expert_id": "KSJ", "name": "전문가 C"}, path=tmp_db)
        for sid, expert in [("YJH_US10Y", "YJH"), ("KSJ_US10Y_HIGH", "KSJ")]:
            store.upsert_signal({
                "signal_id": sid, "name": sid, "expert_id": expert, "kind": "regime_filter",
                "direction": "risk", "description": "test",
            }, path=tmp_db)
            idx = pd.bdate_range("2024-01-01", periods=800)
            # 최근 30영업일은 계속 on(오늘도 on) -> evaluate_signal이 n_events>=1로 처리
            rows = [
                {"date": d.strftime("%Y-%m-%d"), "signal_id": sid, "value": 1.0, "is_on": int(i >= len(idx) - 30)}
                for i, d in enumerate(idx)
            ]
            store.upsert_signal_values(rows, path=tmp_db)
        store.insert_weight_history([
            {"recomputed_on": "2024-01-01", "signal_id": "YJH_US10Y", "old_weight_pct": None, "new_weight_pct": 30.0, "reason": "test"},
            {"recomputed_on": "2024-01-01", "signal_id": "KSJ_US10Y_HIGH", "old_weight_pct": None, "new_weight_pct": 30.0, "reason": "test"},
        ], path=tmp_db)

        ctx = build_dashboard_context()
        warnings = ctx["risk_state"]["concentration_warnings"]
        assert len(warnings) == 1
        assert warnings[0]["group"] == "US10Y(^TNX)"
        assert set(warnings[0]["signals"]) == {"YJH_US10Y", "KSJ_US10Y_HIGH"}
