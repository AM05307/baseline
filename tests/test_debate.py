"""
관점별 토론 섀도 기록 — 가짜 클라이언트로만 검증(실제 Anthropic API 호출 없음).

2라운드 전체 흐름 · 같은 스냅샷 · 메모 요약은 사이클 관점에만 · 가드레일 위반/잘못된 결론/거절 → 미저장·검토 대기 ·
예산 초과 → 미실행(클라이언트 생성조차 안 함) · dry-run 기본 · 오래된 데이터·과거 날짜 옵션 없음 · 섀도 기록 멱등 ·
executor·위험 점수 격리 · 대시보드 카드 · 채점(표본 60 미만 판단 불가).
"""

from __future__ import annotations

import ast
import json
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agents import debate, memo_summary  # noqa: E402
from db import store  # noqa: E402
from scripts import debate_scorecard, run_debate  # noqa: E402

SNAP = debate.snapshot_from_data({
    "price_000660": {"as_of": "2026-10-16", "close": 1272000.0, "return_20d_pct": -25.0, "samples": 252},
    "rates": {"us10y_pct": 5.1}, "market_sentiment": {"error": "VIX 데이터 없음"},
})
MEMO = {"url": "https://x/insights/memo/fake", "title": "Fake Memo", "published_on": "2026-10-01",
        "summary": "가짜 요약: 메모는 사이클과 심리를 다룬다."}
GOOD_SYNTH = '정리합니다.\n{"conclusion": "위험 높음", "evidence": ["price_000660", "rates"], "summary": "20일 수익률 -25%."}'
CFG = {"model": "claude-opus-5-5", "effort": "medium", "max_tokens_per_call": 1000, "refusal_fallback": True,
       "max_tokens_per_run": 200000, "monthly_budget_usd": 50.0, "price_per_mtok": {"input": 4.0, "output": 20.0},
       "max_snapshot_age_days": 5}


class FakeClient:
    """system·user로 누구의 호출인지 보고 미리 정한 답을 준다. 호출을 기록한다."""

    def __init__(self, override=None, synth=GOOD_SYNTH, stop="end_turn"):
        self.calls = []
        self.override = override or {}
        self.synth = synth
        self.stop = stop
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        system, user = kw["system"], kw["messages"][0]["content"]
        if "토론 종합자" in system:
            text = self.synth
        elif "개인 참고용으로 투자 메모" in system:
            text = "메모는 시장 사이클과 투자자 심리의 관계를 다룬다."
        else:
            label = re.search(r"토론 참가자 '([^']+)'", system).group(1)
            rnd = "2라운드" if "2라운드:" in user else "1라운드"
            text = self.override.get((label, rnd), f"{label} {rnd} 의견: price_000660 기준 관측값 해석.")
        return {"content": [{"type": "text", "text": text}], "stop_reason": self.stop,
                "usage": {"input_tokens": 100, "output_tokens": 50}, "model": kw["model"]}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "t.db"
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init_db(p)
    return p


def _main(db, argv=(), client=None, snap=SNAP, cfg=CFG, today=date(2026, 10, 18), **kw):
    made = []

    def factory():
        made.append(1)
        return client or FakeClient()
    rc = run_debate.main(list(argv), client_factory=factory, snapshot_fn=lambda: snap, today=today, cfg=cfg,
                         path=db, **kw)
    return rc, made


class TestFlow:
    def test_two_rounds_then_synthesis_on_one_snapshot(self):
        c = FakeClient()
        res = debate.run_debate(c, SNAP, MEMO, debate.DebateConfig(max_tokens_per_call=1000))
        assert res.passed and res.conclusion == "위험 높음" and res.evidence == ["price_000660", "rates"]
        assert len(c.calls) == 10 == res.calls  # 1라운드 4 + 2라운드 5 + 종합 1
        assert set(res.round1) == {"cycle", "rates", "value", "trend"}  # 반대 관점은 1라운드 의견 없음
        assert set(res.round2) == {"cycle", "rates", "value", "trend", "contrarian"}
        users = [k["messages"][0]["content"] for k in c.calls]
        assert all(SNAP.json_text in u for u in users)  # 모두 같은 스냅샷
        memo_in = [u for u in users if "가짜 요약" in u]
        assert len(memo_in) == 1 and "1라운드" in memo_in[0]  # 메모 요약은 사이클 관점 1라운드에만
        contrarian_r2 = next(u for k, u in zip(c.calls, users) if "'반대 관점'" in k["system"])
        assert all(f"{lab} 1라운드 의견" in contrarian_r2 for lab in ("사이클 관점", "금리·유동성 관점", "가치 관점", "추세 관점"))
        cycle_r2 = next(u for k, u in zip(c.calls, users) if "'사이클 관점'" in k["system"] and "2라운드:" in u)
        assert "사이클 관점 1라운드 의견" not in cycle_r2  # 자기 의견은 빼고 남의 의견만
        assert (res.input_tokens, res.output_tokens) == (1000, 500)
        assert all(k["output_config"] == {"effort": "medium"} and "temperature" not in k for k in c.calls)

    def test_real_client_shape_uses_server_side_fallback(self):
        seen = {}

        class Beta:
            class messages:  # noqa: N801
                @staticmethod
                def create(**kw):
                    seen.update(kw)
                    return {"content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn"}

        class Real:
            beta = Beta()
        debate._create(Real(), debate.DebateConfig(), "s", "u")
        assert seen["betas"] == [debate.FALLBACK_BETA] and seen["fallbacks"] == "default"


class TestGuardrails:
    @pytest.mark.parametrize("override,synth,stop,needle", [
        ({("추세 관점", "1라운드"): "추세상 앞으로 오를 것으로 보인다."}, GOOD_SYNTH, "end_turn", "오를 것"),
        ({("가치 관점", "2라운드"): "막스가 말했다: 지금은 싸다."}, GOOD_SYNTH, "end_turn", "가 말했다"),
        ({}, '{"conclusion": "매수", "evidence": ["rates"], "summary": "x"}', "end_turn", "3단계"),
        ({}, '{"conclusion": "중립", "evidence": ["made_up_key"], "summary": "x"}', "end_turn", "made_up_key"),
        ({}, "결론 없음", "end_turn", "JSON"),
        ({}, GOOD_SYNTH, "refusal", "refusal"),
        ({}, GOOD_SYNTH, "max_tokens", "잘림"),
    ])
    def test_violation_is_not_saved_and_goes_to_review(self, db, override, synth, stop, needle):
        rc, _ = _main(db, ["--execute"], client=FakeClient(override, synth, stop))
        assert rc == 2
        assert store.get_debate_shadow(db).empty
        rev = store.get_debate_review(db)
        assert len(rev) == 1 and needle in rev.iloc[0]["warnings"]

    def test_clean_run_is_saved_with_all_audit_fields(self, db):
        rc, _ = _main(db, ["--execute"])
        assert rc == 0
        row = store.get_debate_shadow(db).iloc[0]
        assert (row["decision_date"], row["snapshot_hash"], row["model"]) == ("2026-10-16", SNAP.hash, "claude-opus-5-5")
        assert row["conclusion"] == "위험 높음" and json.loads(row["guardrail"])["passed"] is True
        assert set(json.loads(row["opinions"])) == {"round1", "round2"} and row["input_tokens"] == 1000


class TestSafetyAndCost:
    def test_default_is_dry_run_and_never_builds_a_client(self, db, capsys):
        rc, made = _main(db, [])
        assert rc == 0 and made == [] and store.get_debate_shadow(db).empty
        assert "DRY-RUN" in capsys.readouterr().out

    def test_per_run_token_cap_blocks_execution(self, db):
        rc, made = _main(db, ["--execute"], cfg={**CFG, "max_tokens_per_run": 1000})
        assert rc == 3 and made == []

    def test_monthly_budget_blocks_execution(self, db):
        store.insert_debate_shadow({"decision_date": "2026-10-01", "snapshot_hash": "h", "model": "m", "opinions": "{}",
                                    "conclusion": "중립", "evidence": "[]", "input_tokens": 1_000_000,
                                    "output_tokens": 100_000, "guardrail": "{}"}, path=db)  # 이번 달 $6
        rc, made = _main(db, ["--execute"], cfg={**CFG, "monthly_budget_usd": 6.1})
        assert rc == 3 and made == []

    def test_stale_snapshot_and_no_past_date_option(self, db):
        old = debate.snapshot_from_data({"price_000660": {"as_of": "2026-09-01"}})
        assert _main(db, ["--execute"], snap=old)[0] == 1
        with pytest.raises(SystemExit):
            _main(db, ["--date", "2024-01-02"])
        assert "decision_date" not in debate.run_debate.__code__.co_varnames  # 날짜를 받는 인자가 없다

    def test_shadow_record_is_idempotent(self, db):
        assert _main(db, ["--execute"])[0] == 0
        rc, made = _main(db, ["--execute"])
        assert rc == 0 and made == [] and len(store.get_debate_shadow(db)) == 1  # 같은 기준일·스냅샷·모델은 다시 안 돌림
        row = debate.shadow_row(debate.run_debate(FakeClient(), SNAP, None, debate.DebateConfig()), None)
        assert store.insert_debate_shadow(row, path=db) is False


class TestMemoSummary:
    def test_verbatim_copy_is_rejected(self):
        body = "This placeholder sentence is long enough to be copied verbatim into a summary text."

        class Copier(FakeClient):
            def create(self, **kw):
                return {"content": [{"type": "text", "text": body}], "stop_reason": "end_turn"}
        summary, problems, _ = memo_summary.summarize(Copier(), debate.DebateConfig(), "t", body)
        assert summary is None and any("40자" in p for p in problems)

    def test_execute_summarizes_memo_once_without_storing_body(self, db):
        store.upsert_memo({"url": MEMO["url"], "title": "Fake Memo", "published_on": "2026-10-01"}, path=db)
        body = "BODY-TEXT placeholder about cycles " * 20
        rc, _ = _main(db, ["--execute"], memo_text_fn=lambda url: body)
        assert rc == 0
        memo = store.latest_memo(db)
        assert memo["summary"].startswith("메모는") and memo["summary_by"] == "claude-opus-5-5"
        assert "BODY-TEXT" not in json.dumps(memo, ensure_ascii=False)
        assert store.get_debate_shadow(db).iloc[0]["memo_url"] == MEMO["url"]


class TestIsolation:
    def test_executor_never_reads_or_imports_debate(self):
        for f in (ROOT / "executor").rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            assert "debate" not in text.lower(), f
            mods = {n.module for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ImportFrom) and n.module}
            mods |= {a.name for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Import) for a in n.names}
            assert not any(m.split(".")[0] == "agents" for m in mods), f

    def test_risk_score_code_never_references_debate(self):
        for f in list((ROOT / "analysis").glob("*.py")) + [ROOT / "scripts" / "what_if_recent.py",
                                                         ROOT / "scripts" / "compute_signals.py",
                                                         ROOT / "scripts" / "recompute_weights.py"]:
            assert "debate" not in f.read_text(encoding="utf-8").lower(), f

    def test_dashboard_risk_state_is_identical_with_or_without_debate_records(self, db):
        import test_dashboard_render as tdr
        from web import main as web_main
        tdr._seed_everything(db)
        before = web_main.build_dashboard_context()["risk_state"]
        store.insert_debate_shadow({"decision_date": "2026-10-16", "snapshot_hash": "h", "model": "m",
                                    "opinions": "{}", "conclusion": "위험 낮음", "evidence": '["rates"]',
                                    "synthesis": "가짜 종합", "guardrail": "{}"}, path=db)
        ctx = web_main.build_dashboard_context()
        assert ctx["risk_state"] == before
        assert ctx["debate"]["conclusion"] == "위험 낮음"
        from fastapi.testclient import TestClient
        html = TestClient(web_main.app).get("/").text
        assert "관점별 토론 (섀도 기록 — 매매 미반영)" in html and "가짜 종합" in html


class TestScorecard:
    def _rows(self, n):
        dates = pd.bdate_range("2025-01-01", periods=n)
        return pd.DataFrame({"decision_date": [d.date().isoformat() for d in dates],
                             "conclusion": ["위험 높음" if i % 2 else "위험 낮음" for i in range(n)]}), dates

    def test_small_sample_is_undecidable_but_shows_n_and_worst(self):
        rows, dates = self._rows(20)
        close = pd.Series(np.linspace(100, 120, len(dates) + 80), index=pd.bdate_range("2025-01-01", periods=len(dates) + 80))
        sc = debate_scorecard.compute_scorecard(rows, close, {}, (5,), 60)
        r = {x["conclusion"]: x for x in sc["returns"]}
        assert r["위험 높음"]["verdict"] == "판단 불가" and r["위험 높음"]["n"] == 10 and r["위험 높음"]["worst"] is not None
        assert r["중립"]["n"] == 0 and r["중립"]["worst"] is None

    def test_enough_samples_and_agreement_rate(self):
        rows, dates = self._rows(130)
        close = pd.Series(np.linspace(100, 150, len(dates) + 80), index=pd.bdate_range("2025-01-01", periods=len(dates) + 80))
        states = {d: ("2단계 이상" if c == "위험 높음" else "1단계") for d, c in zip(rows["decision_date"], rows["conclusion"])}
        sc = debate_scorecard.compute_scorecard(rows, close, states, (5,), 60,
                                                {"정상": "위험 낮음", "1단계": "중립", "2단계 이상": "위험 높음"})
        assert {x["verdict"] for x in sc["returns"] if x["n"]} == {"표본 충분"}
        a = sc["agreement"]
        assert a["n"] == 130 and a["matches"] == 65 and a["rate"] == 0.5
