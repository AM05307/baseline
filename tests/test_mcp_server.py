"""
MCP 툴 로직 테스트.

mcp_server/tools.py는 mcp 패키지에 의존하지 않는 순수 함수라 mcp SDK가
없어도 이 파일은 그대로 돈다. mcp_server/server.py(FastMCP 배선)는
mcp가 설치돼 있을 때만 별도로 확인한다 (importorskip).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from mcp_server import tools  # noqa: E402


def _fake_prices(ticker: str, days: int = 260, seed: int = 0) -> list[dict]:
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range("2024-01-01", periods=days)
    closes = np.cumprod(1 + rs.normal(0, 0.01, days)) * 100
    return [
        {
            "ticker": ticker, "date": d.strftime("%Y-%m-%d"),
            "open": float(c), "high": float(c), "low": float(c), "close": float(c),
            "volume": 1000, "source": "test",
        }
        for d, c in zip(idx, closes)
    ]


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


class TestGetCorrelation:
    def test_missing_ticker_raises_data_not_found(self, tmp_db):
        with pytest.raises(tools.DataNotFoundError):
            tools.get_correlation("NVDA")

    def test_returns_samples_and_reliable_flag(self, tmp_db):
        store.upsert_prices(_fake_prices("SOX", seed=1), path=tmp_db)
        store.upsert_prices(_fake_prices("NVDA", seed=2), path=tmp_db)
        r = tools.get_correlation("NVDA", "SOX", window=60)
        assert r["samples"] == 60
        assert "reliable" in r
        assert r["note"]  # 예측 금지 안내 문구가 항상 있어야 한다

    def test_never_emits_forecast_language(self, tmp_db):
        store.upsert_prices(_fake_prices("SOX", seed=1), path=tmp_db)
        store.upsert_prices(_fake_prices("NVDA", seed=2), path=tmp_db)
        r = tools.get_correlation("NVDA", "SOX")
        blob = str(r)
        for phrase in ["오를 것", "매수 시점", "매도 시점"]:
            assert phrase not in blob


class TestGetDivergenceDays:
    def test_shape(self, tmp_db):
        store.upsert_prices(_fake_prices("SOX", days=400, seed=1), path=tmp_db)
        store.upsert_prices(_fake_prices("NVDA", days=400, seed=2), path=tmp_db)
        r = tools.get_divergence_days("NVDA", "SOX", limit=5)
        assert r["count"] == len(r["days"])
        assert r["count"] <= 5
        for day in r["days"]:
            assert {"date", "ticker_change_pct", "benchmark_change_pct", "z"} <= day.keys()


class TestGetCalendarEvents:
    def test_no_db_needed(self, tmp_db):
        r = tools.get_calendar_events(days=60)
        assert "events" in r
        assert isinstance(r["events"], list)


class TestGetBacktest:
    def test_unsupported_event_rejected(self, tmp_db):
        store.upsert_prices(_fake_prices("SOX", days=800, seed=1), path=tmp_db)
        with pytest.raises(ValueError):
            tools.get_backtest("SOX", event="earnings_drift")

    def test_reports_n_and_worst(self, tmp_db):
        store.upsert_prices(_fake_prices("SOX", days=800, seed=1), path=tmp_db)
        r = tools.get_backtest("SOX")
        assert "n" in r and "worst_pct" in r
        assert "reliable" in r


class TestGetCollectionStatus:
    def test_reports_kis_ready_flag(self, tmp_db):
        r = tools.get_collection_status()
        assert "kis_ready" in r
        assert "watch_us" in r

    def test_no_nan_leaks_into_recent_runs(self, tmp_db):
        """recent_runs가 DataFrame.to_dict을 그대로 안 쓰는지 (nan 문자열 버그 회귀 방지)."""
        store.log_run("us_market", "ok", 10, message=None, path=tmp_db)
        r = tools.get_collection_status()
        for run in r["recent_runs"]:
            assert run.get("message") != "nan"


class TestGetAdrPremium:
    def test_computes_premium_with_real_ratio(self, tmp_db):
        store.upsert_prices(_fake_prices("000660", days=60, seed=1), path=tmp_db)
        store.upsert_prices(_fake_prices("SKHY", days=60, seed=2), path=tmp_db)
        store.upsert_prices(_fake_prices("USDKRW", days=60, seed=3), path=tmp_db)
        r = tools.get_adr_premium()
        assert r["ratio"] == 10
        assert "premium_pct" in r

    def test_missing_data_raises(self, tmp_db):
        with pytest.raises(tools.DataNotFoundError):
            tools.get_adr_premium()


class TestGetMarketSentiment:
    def test_shape_and_bounds(self, tmp_db):
        store.upsert_prices(_fake_prices("VIX", days=300, seed=9), path=tmp_db)
        r = tools.get_market_sentiment()
        assert 0 <= r["gauge_score"] <= 100
        assert "category" in r


class TestGetNews:
    def test_filters_by_ticker_and_avoids_nan(self, tmp_db):
        store.upsert_news([
            {"ticker": "000660", "headline": "테스트", "source": None, "url": None,
             "published_at": "2026-08-20", "confidence": "rumor"},
        ], path=tmp_db)
        r = tools.get_news(ticker="000660")
        assert r["count"] == 1
        assert r["items"][0]["source"] is None  # "nan" 문자열이 아니라 진짜 None


class TestGetFrameworkCheck:
    def test_returns_six_lenses_without_impersonation(self, tmp_db):
        store.upsert_prices(_fake_prices("000660", days=800, seed=1), path=tmp_db)
        store.upsert_prices(_fake_prices("SOX", days=800, seed=2), path=tmp_db)
        r = tools.get_framework_check(ticker="000660")
        assert len(r["lenses"]) == 6
        blob = str(r)
        # 실존 인물 이름이 1인칭 발언 형태로 들어가지 않았는지 (이름 자체는
        # 프레임워크 라벨에 등장할 수 있으므로, 금지어는 1인칭 서술 패턴만 검사)
        for phrase in ["버핏이 말하길", "린치가 말하길", "버핏은 이렇게 본다"]:
            assert phrase not in blob


class TestGetClaimScoreboard:
    def test_empty_reports_zero_samples(self, tmp_db):
        r = tools.get_claim_scoreboard()
        assert r["n"] == 0
        assert r["hit_rate_pct"] is None
        assert r["reliable"] is False
        assert r["note"]

    def test_reports_graded_claims_with_samples(self, tmp_db):
        store.insert_claim({
            "said_on": "2026-01-01", "who": "테스트", "ticker": "000660",
            "summary": "요약", "price_then": 100, "target_price": 120,
            "horizon_days": 30, "confidence": "news",
        }, path=tmp_db)
        claim_id = int(store.get_claims_for_grading(path=tmp_db).iloc[0]["id"])
        store.grade_claim(claim_id, "2026-01-31", 130.0, 1, path=tmp_db)

        r = tools.get_claim_scoreboard()
        assert r["n"] == 1
        assert r["hits"] == 1
        assert r["hit_rate_pct"] == 100.0
        assert len(r["recent"]) == 1


class TestGetRuleBacktest:
    def _fake_ohlc(self, ticker: str, days: int = 400, seed: int = 0) -> list[dict]:
        rs = np.random.RandomState(seed)
        idx = pd.bdate_range("2023-01-01", periods=days)
        closes = np.cumprod(1 + rs.normal(0, 0.01, days)) * 100
        return [
            {
                "ticker": ticker, "date": d.strftime("%Y-%m-%d"),
                "open": float(c) * 0.999, "high": float(c), "low": float(c),
                "close": float(c), "volume": 1000, "source": "test",
            }
            for d, c in zip(idx, closes)
        ]

    def test_insufficient_data_raises(self, tmp_db):
        store.upsert_prices(self._fake_ohlc("000660", days=50), path=tmp_db)
        with pytest.raises(tools.DataNotFoundError):
            tools.get_rule_backtest("000660")

    def test_unknown_strategy_rejected(self, tmp_db):
        store.upsert_prices(self._fake_ohlc("000660", days=400), path=tmp_db)
        with pytest.raises(ValueError):
            tools.get_rule_backtest("000660", strategy="golden_cross")

    def test_returns_full_in_sample_out_of_sample_with_reliability(self, tmp_db):
        store.upsert_prices(self._fake_ohlc("000660", days=400), path=tmp_db)
        r = tools.get_rule_backtest("000660", strategy="ma_cross")
        assert set(r) >= {"full", "in_sample", "out_of_sample", "note"}
        for window in ("full", "in_sample", "out_of_sample"):
            assert "n" in r[window] and "reliable" in r[window]
            assert "mdd_pct" in r[window]  # 원칙 4: MDD가 항상 같이 있어야 한다

    def test_ensemble_threshold_is_echoed_back(self, tmp_db):
        store.upsert_prices(self._fake_ohlc("000660", days=400), path=tmp_db)
        r = tools.get_rule_backtest("000660", strategy="ensemble", threshold=0.75)
        assert r["threshold"] == 0.75
        r2 = tools.get_rule_backtest("000660", strategy="ma_cross")
        assert r2["threshold"] is None  # ensemble이 아니면 의미 없는 값

    def test_never_emits_forecast_or_trade_language(self, tmp_db):
        store.upsert_prices(self._fake_ohlc("000660", days=400), path=tmp_db)
        r = tools.get_rule_backtest("000660", strategy="macd_cross")
        blob = str(r)
        for phrase in ["오를 것", "지금 사세요", "매수 시점", "매도 시점"]:
            assert phrase not in blob


def test_server_module_registers_all_tools_when_mcp_installed():
    """mcp SDK가 있는 환경에서만: server.py가 12개 툴을 등록하는지 확인."""
    pytest.importorskip("mcp")
    import asyncio

    from mcp_server import server  # noqa: PLC0415

    # list_tools()는 FastMCP가 노출하는 공개 코루틴 — 실제 MCP 클라이언트가
    # ListTools 요청 시 받는 것과 동일한 목록이다.
    tool_list = asyncio.run(server.mcp.list_tools())
    tool_names = {t.name for t in tool_list}
    expected = {
        "get_correlation", "get_divergence_days", "get_calendar_events",
        "get_backtest", "get_collection_status", "get_adr_premium",
        "get_market_sentiment", "get_news", "get_framework_check",
        "get_agent_analysis", "get_claim_scoreboard", "get_rule_backtest",
    }
    assert expected <= tool_names
