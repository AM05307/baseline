"""
agents/orchestrator.py 테스트.

orchestrator.py는 anthropic 패키지를 import하지 않는다(덕 타이핑 클라이언트) —
그래서 이 테스트는 anthropic 설치 여부와 무관하게 항상 돌고, 진짜 API를
절대 호출하지 않는다. 여기서는 미리 정해둔 응답을 순서대로 돌려주는
가짜 클라이언트로 tool-use 루프·가드레일 로직만 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import orchestrator  # noqa: E402
from db import store  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _fake_prices(ticker: str, days: int = 800, seed: int = 0, base: float = 100.0) -> list[dict]:
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range("2024-01-01", periods=days)
    closes = np.cumprod(1 + rs.normal(0, 0.01, days)) * base
    return [
        {"ticker": ticker, "date": d.strftime("%Y-%m-%d"),
         "open": float(c), "high": float(c), "low": float(c), "close": float(c),
         "volume": 1000, "source": "test"}
        for d, c in zip(idx, closes)
    ]


def _seed_prices(tmp_db):
    store.upsert_prices(_fake_prices("000660", seed=1, base=312000), path=tmp_db)
    store.upsert_prices(_fake_prices("SOX", seed=2, base=4000), path=tmp_db)


class FakeBlock:
    """anthropic SDK의 content block을 흉내낸다 — 속성 접근(.type/.text/...)만 지원."""

    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class FakeUsage:
    def __init__(self, input_tokens=10, output_tokens=20):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, content, stop_reason, usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage()


class ScriptedClient:
    """미리 정해둔 응답 목록을 호출 순서대로 반환하는 가짜 Anthropic 클라이언트.
    진짜 네트워크 호출도, anthropic 패키지 의존도 없다."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            return self._outer._responses.pop(0)

    @property
    def messages(self):
        return self._Messages(self)


class TestAnalyzeTicker:
    def test_single_turn_no_tool_use(self, tmp_db):
        client = ScriptedClient([
            FakeResponse([FakeBlock("text", text="관측값만 서술한 요약입니다.")], "end_turn"),
        ])
        result = orchestrator.analyze_ticker(client, ticker="000660")
        assert result.narrative == "관측값만 서술한 요약입니다."
        assert result.tool_calls == []
        # 툴을 한 번도 안 부르면 "근거 없이 작성" 경고가 붙어야 한다
        assert any("툴" in w for w in result.warnings)

    def test_tool_use_then_final_answer(self, tmp_db):
        _seed_prices(tmp_db)
        client = ScriptedClient([
            FakeResponse(
                [FakeBlock("tool_use", id="t1", name="get_framework_check", input={"ticker": "000660"})],
                "tool_use",
            ),
            FakeResponse([FakeBlock("text", text="프레임워크 체크 결과를 근거로 정리했습니다.")], "end_turn"),
        ])
        result = orchestrator.analyze_ticker(client, ticker="000660")
        assert result.iterations == 2
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0]["name"] == "get_framework_check"
        assert "lenses" in result.tool_calls[0]["result"]
        assert result.narrative == "프레임워크 체크 결과를 근거로 정리했습니다."
        assert not any("툴" in w for w in result.warnings)

    def test_missing_data_tool_call_returns_error_not_crash(self, tmp_db):
        # 빈 DB에서 get_framework_check을 부르면 DataNotFoundError가 나는데,
        # 루프를 죽이지 않고 error 필드로 담겨서 다음 턴으로 넘어가야 한다.
        client = ScriptedClient([
            FakeResponse(
                [FakeBlock("tool_use", id="t1", name="get_framework_check", input={"ticker": "000660"})],
                "tool_use",
            ),
            FakeResponse([FakeBlock("text", text="데이터가 없어 확인할 수 없습니다.")], "end_turn"),
        ])
        result = orchestrator.analyze_ticker(client, ticker="000660")
        assert "error" in result.tool_calls[0]["result"]

    def test_banned_forecast_phrase_flagged_as_warning(self, tmp_db):
        _seed_prices(tmp_db)
        client = ScriptedClient([
            FakeResponse(
                [FakeBlock("tool_use", id="t1", name="get_framework_check", input={"ticker": "000660"})],
                "tool_use",
            ),
            FakeResponse([FakeBlock("text", text="000660은 내일 오를 것으로 보입니다.")], "end_turn"),
        ])
        result = orchestrator.analyze_ticker(client, ticker="000660")
        assert any("오를 것" in w for w in result.warnings)

    def test_impersonation_quote_pattern_flagged(self, tmp_db):
        _seed_prices(tmp_db)
        client = ScriptedClient([
            FakeResponse(
                [FakeBlock("tool_use", id="t1", name="get_framework_check", input={"ticker": "000660"})],
                "tool_use",
            ),
            FakeResponse([FakeBlock("text", text="워런 버핏이 말하길 좋은 기업이라고 합니다.")], "end_turn"),
        ])
        result = orchestrator.analyze_ticker(client, ticker="000660")
        assert any("말하길" in w for w in result.warnings)

    def test_max_iterations_cutoff_does_not_hang(self, tmp_db):
        _seed_prices(tmp_db)
        responses = [
            FakeResponse(
                [FakeBlock("tool_use", id=f"t{i}", name="get_market_sentiment", input={})],
                "tool_use",
            )
            for i in range(10)
        ]
        client = ScriptedClient(responses)
        result = orchestrator.analyze_ticker(client, ticker="000660", max_tool_iterations=3)
        assert result.iterations == 3
        assert "최대 반복" in result.narrative

    def test_usage_tokens_summed_across_turns(self, tmp_db):
        _seed_prices(tmp_db)
        client = ScriptedClient([
            FakeResponse(
                [FakeBlock("tool_use", id="t1", name="get_framework_check", input={"ticker": "000660"})],
                "tool_use", usage=FakeUsage(input_tokens=100, output_tokens=50),
            ),
            FakeResponse([FakeBlock("text", text="정리했습니다.")], "end_turn",
                         usage=FakeUsage(input_tokens=120, output_tokens=80)),
        ])
        result = orchestrator.analyze_ticker(client, ticker="000660")
        assert result.input_tokens == 220
        assert result.output_tokens == 130


class TestToolSpecsMatchDispatch:
    def test_every_tool_spec_has_a_dispatch_entry(self):
        spec_names = {t["name"] for t in orchestrator.TOOL_SPECS}
        assert spec_names == set(orchestrator._TOOL_DISPATCH.keys())


class TestAgentAnalysisStorage:
    def test_upsert_and_get_latest_is_idempotent(self, tmp_db):
        store.upsert_agent_analysis({
            "ticker": "000660", "run_date": "2026-08-23", "model": "test-model",
            "narrative": "첫 버전", "tool_calls": "[]", "warnings": "[]",
        }, path=tmp_db)
        store.upsert_agent_analysis({
            "ticker": "000660", "run_date": "2026-08-23", "model": "test-model",
            "narrative": "같은 날 재실행 — 갱신됨", "tool_calls": "[]", "warnings": "[]",
        }, path=tmp_db)
        row = store.get_latest_agent_analysis("000660", path=tmp_db)
        assert row["narrative"] == "같은 날 재실행 — 갱신됨"
        with store.conn(tmp_db) as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM agent_analyses WHERE ticker='000660'"
            ).fetchone()["n"]
        assert n == 1  # 원칙 6: 같은 날 재실행해도 행이 늘지 않는다

    def test_get_latest_returns_none_when_absent(self, tmp_db):
        assert store.get_latest_agent_analysis("NVDA", path=tmp_db) is None


class TestMcpGetAgentAnalysis:
    def test_returns_unavailable_without_cache(self, tmp_db):
        from mcp_server import tools
        r = tools.get_agent_analysis("000660")
        assert r["available"] is False

    def test_returns_cached_narrative(self, tmp_db):
        from mcp_server import tools
        store.upsert_agent_analysis({
            "ticker": "000660", "run_date": "2026-08-23", "model": "test-model",
            "narrative": "캐싱된 요약", "tool_calls": '[{"name": "get_framework_check"}]',
            "warnings": "[]",
        }, path=tmp_db)
        r = tools.get_agent_analysis("000660")
        assert r["available"] is True
        assert r["narrative"] == "캐싱된 요약"
        assert r["tool_calls"][0]["name"] == "get_framework_check"
