"""
FakeBroker 시나리오 — 자본 사다리 전체 흐름(네트워크·실제 주문 없음).

한도 강제 · 한도 초과 계획 거부 · 손실 미보전 · 승격 거부(기준 미충족/미확정/확인 문구) · MDD 초과 자동 강등과
같은 실행 안의 자본 축소 · 주문 오류·잔고 불일치 강등과 정지 · 자동 재승격 없음 · 중복 실행 · 부분 체결·체결 지연 ·
append-only · 적립 기록 · 자금 배분 화면.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import engine, ladder_ops, store  # noqa: E402
from executor.broker.fake import FakeBroker  # noqa: E402
from executor.planner import Plan, PlannedOrder  # noqa: E402
from executor.scripts import add_contribution, promotion_report, resume  # noqa: E402
from executor.tiers import parse_ladder  # noqa: E402
from executor.web import build_allocation_context  # noqa: E402

T = "000660"
DEMOTE = {"max_mdd_pct": 15, "max_divergence_pct": 50, "max_order_errors": 0, "max_reconciliation_mismatches": 0}
LADDER = parse_ladder({"tiers": [
    {"name": "shadow", "kind": "virtual", "max_capital_krw": 2_000_000, "confirmed": True, "promotion_criteria": "s2p"},
    {"name": "paper", "kind": "virtual", "max_capital_krw": 2_000_000, "confirmed": True, "promotion_criteria": "p2l",
     "demotion": DEMOTE},
    {"name": "live_1", "kind": "real", "max_capital_krw": 2_000_000, "confirmed": True, "promotion_criteria": "l2l",
     "demotion": DEMOTE},
    {"name": "live_2", "kind": "real", "max_capital_krw": "미확정", "confirmed": False, "demotion": DEMOTE},
]})
EASY = {"confirmed": True, "min_days": 2, "min_trades": 0, "max_order_errors": 0, "max_reconciliation_mismatches": 0}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "executor.db"
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init_db(p)
    return p


def _enter_live_1(db, date="2026-11-02", account_value=6_000_000):
    """테스트 준비용: 승격 절차를 건너뛰고 live_1에 들어간 상태를 만든다(실제 경로는 promote())."""
    ladder_ops.ensure_initialized(LADDER, "2026-10-01", db)
    store.append_tier_event(date, "promote", "paper", "live_1", "manual", "테스트 준비", path=db)
    store.append_ledger(date, "real", "allocate", min(account_value, 2_000_000), note="테스트 배정", path=db)


def _run(broker, date, price, weight=1.0, db=None, **kw):
    broker.prices[T] = price
    kw.setdefault("fill", "same_close")  # 이 파일은 즉시 체결 기준으로 한도·강등 규칙을 검증한다(next_open은 test_executor_fill)
    return engine.run_once(broker, LADDER, date, {T: weight}, {T: price}, path=db, **kw)


def _buys(broker):
    return [c for c in broker.calls if c[0] == "place_order"]


# ---------------------------------------------------------------- 1·2. 한도 강제

class TestCapEnforcement:
    def test_balance_three_times_cap_never_orders_more_than_cap(self, db):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        r = _run(b, "2026-11-02", 10_000, db=db)
        assert r.status == "ok" and r.planning_capital == 2_000_000
        o = store.orders("real", db)
        notional = float((o["qty"] * o["ref_price"]).sum())
        assert 0 < notional <= 2_000_000
        assert b.cash >= 4_000_000  # 한도 밖 금액은 현금 그대로
        snap = store.snapshots("real", path=db).iloc[-1]
        assert snap["outside_cash"] == pytest.approx(snap["account_value"] - snap["operating_capital"])
        assert snap["outside_cash"] == pytest.approx(4_000_000)  # 수수료는 운용분에서 빠지고 한도 밖 현금은 그대로

    def test_plan_exceeding_cap_is_rejected_and_nothing_is_sent(self, db, monkeypatch):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        monkeypatch.setattr(engine, "plan_orders", lambda *a, **k: Plan(capital=2_000_000, orders=[
            PlannedOrder(T, "buy", 300, 10_000.0)]))  # 300만원 — 한도 200만원 초과
        r = _run(b, "2026-11-02", 10_000, db=db)
        assert r.status == "cap_rejected" and "운용 자본" in r.message
        assert _buys(b) == [] and store.orders(path=db).empty
        assert "cap_rejected" in set(store.events(path=db)["kind"])

    def test_weights_over_100pct_rejected(self, db):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        assert _run(b, "2026-11-02", 10_000, weight=1.5, db=db).status == "cap_rejected"
        assert _buys(b) == []

    def test_loss_is_not_refilled_from_outside_cash(self, db):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        _run(b, "2026-11-02", 10_000, db=db)
        r = _run(b, "2026-11-03", 9_000, db=db)  # −10% (강등 기준 15% 미만)
        assert r.tier == "live_1"
        assert r.planning_capital < 1_810_000  # 계좌엔 400만원 넘게 있지만 운용분 손실 그대로(200만원으로 안 채움)
        exposure = b.positions[T] * 9_000
        assert exposure <= r.planning_capital  # 같은 운용분 안에서 비중만 맞춤(가격이 내려 1주 더 살 수는 있음)


# ---------------------------------------------------------------- 3. 강등(자동)

class TestDemotion:
    def test_mdd_breach_demotes_freezes_new_orders_and_keeps_holdings(self, db):
        """2026-10-18 결정: MDD·괴리 강등은 신규 주문 정지 + 보유 유지 + 수동 판단 필요(자동 정리 안 함)."""
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        _run(b, "2026-11-02", 10_000, db=db)
        held = b.positions[T]
        calls = len(_buys(b))
        r = _run(b, "2026-11-03", 8_000, db=db)  # 운용분 −20% > 15%
        assert r.demoted_to == ["paper"] and r.tier == "paper" and r.status == "review_required"
        assert r.operating_capital == 0  # 계산상 실제 자금 운용 자본은 즉시 0
        assert b.positions[T] == held and len(_buys(b)) == calls  # 보유 유지, 주문 없음
        assert store.freeze_state("real", db) == "review_required"
        last = store.tier_history(db).iloc[-1]
        assert (last["event"], last["operator"], last["to_tier"]) == ("demote", "auto", "paper")
        assert "MDD" in last["reason"]
        assert _run(b, "2026-11-04", 7_000, db=db).status == "review_required"  # 다음 날에도 정리 주문 없음
        assert b.positions[T] == held

    def test_divergence_breach_also_freezes_with_holdings_kept(self, db):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        _run(b, "2026-11-02", 10_000, db=db, reference_value=2_000_000)
        held = b.positions[T]
        r = _run(b, "2026-11-03", 9_000, db=db, reference_value=3_000_000)  # 비교 기준 +50%, 실제 −10% → 괴리 60%p
        assert r.status == "review_required" and b.positions[T] == held
        assert "괴리" in store.tier_history(db).iloc[-1]["reason"]

    def test_resume_after_review_lets_next_run_trade_down_to_new_capital(self, db):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        _run(b, "2026-11-02", 10_000, db=db)
        _run(b, "2026-11-03", 8_000, db=db)
        assert resume.main(["--account", "real", "--reason", "정리하기로 결정"], db_path=db) == 0
        r = _run(b, "2026-11-04", 8_000, db=db)
        assert r.status == "ok" and b.positions.get(T, 0) == 0  # 사람이 해제한 뒤에야 정리

    def test_order_error_demotes(self, db):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000}, fail_calls={"place_order": 1})
        r = _run(b, "2026-11-02", 10_000, db=db)
        assert r.tier == "paper" and "주문 오류" in store.tier_history(db).iloc[-1]["reason"]
        assert store.snapshots("real", path=db).iloc[-1]["operating_capital"] == 0

    def test_reconcile_mismatch_halts_demotes_and_needs_manual_resume(self, db):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000}, positions={T: 5})  # 사람이 직접 산 5주
        r = _run(b, "2026-11-02", 10_000, db=db)
        assert r.status == "mismatch" and _buys(b) == []
        assert store.current_tier(db)["to_tier"] == "paper" and store.freeze_state("real", db) == "halt"
        b.positions[T] = 0  # 사람이 직접 판 경우라도 — 정지 중에는 정리 주문 포함 아무 주문도 없음
        assert _run(b, "2026-11-03", 10_000, db=db).status == "halt" and _buys(b) == []
        assert resume.main(["--account", "real", "--reason", "확인함"], db_path=db) == 0
        assert not store.is_halted("real", db)

    def test_no_automatic_repromotion_after_demotion(self, db):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        _run(b, "2026-11-02", 10_000, db=db)
        _run(b, "2026-11-03", 8_000, db=db)
        for i, p in enumerate([9_000, 10_000, 11_000, 12_000]):
            _run(b, f"2026-11-{4 + i:02d}", p, db=db)
        h = store.tier_history(db)
        assert h.iloc[-1]["to_tier"] == "paper"
        assert (h["event"] == "promote").sum() == 1  # 테스트 준비용 1건뿐 — 자동 승격 없음


# ---------------------------------------------------------------- 3. 승격(수동)

def _virtual_days(db, n, start=10_000):
    b = FakeBroker("shadow", cash=2_000_000, prices={T: start})
    for i in range(n):
        _run(b, f"2026-10-{1 + i:02d}", start + 10 * i, db=db)
    return b


class TestPromotion:
    def test_refused_when_criteria_not_met(self, db):
        _virtual_days(db, 3)
        crit = {"stages": {"s2p": {**EASY, "min_days": 50}}}
        with pytest.raises(ladder_ops.PromotionRefused, match="운용 일수"):
            ladder_ops.promote(LADDER, crit, "2026-10-10", "shadow에서 paper로 승격합니다", {T: 10_000}, path=db)
        assert store.current_tier(db)["to_tier"] == "shadow"
        assert store.promotion_results(db).iloc[-1]["eligible"] == 0

    def test_exact_phrase_required_then_applied_as_manual(self, db):
        _virtual_days(db, 3)
        crit = {"stages": {"s2p": EASY}}
        with pytest.raises(ladder_ops.PromotionRefused, match="확인 문구"):
            ladder_ops.promote(LADDER, crit, "2026-10-10", "승격", {T: 10_000}, path=db)
        assert store.current_tier(db)["to_tier"] == "shadow"
        ladder_ops.promote(LADDER, crit, "2026-10-10", "shadow에서 paper로 승격합니다", {T: 10_030}, path=db)
        last = store.tier_history(db).iloc[-1]
        assert (last["event"], last["operator"], last["to_tier"]) == ("promote", "manual", "paper")

    def test_unconfirmed_tier_refused_even_if_all_items_pass(self, db):
        _enter_live_1(db, date="2026-10-01")
        b = FakeBroker("real", cash=2_000_000, prices={T: 10_000})
        for d in ("2026-10-01", "2026-10-02", "2026-10-05"):
            _run(b, d, 10_000, db=db)
        crit = {"stages": {"l2l": EASY}}
        with pytest.raises(ladder_ops.PromotionRefused, match="live_2 미확정"):
            ladder_ops.promote(LADDER, crit, "2026-10-06", "live_1에서 live_2로 승격합니다", {T: 10_000},
                               account_value=9_000_000, path=db)
        assert store.current_tier(db)["to_tier"] == "live_1"

    def test_real_tier_needs_account_value_and_allocates_min_of_account_and_cap(self, db):
        crit = {"stages": {"s2p": EASY, "p2l": EASY}}
        _virtual_days(db, 3)
        ladder_ops.promote(LADDER, crit, "2026-10-04", "shadow에서 paper로 승격합니다", {T: 10_020}, path=db)
        assert store.sleeve_cash("paper", db) == 2_000_000  # 모의투자 계좌는 섀도와 별도 원장
        b = FakeBroker("paper", cash=2_000_000, prices={T: 10_000})
        for d in ("2026-10-05", "2026-10-06"):
            _run(b, d, 10_000, db=db)
        with pytest.raises(ladder_ops.PromotionRefused, match="account-value"):
            ladder_ops.promote(LADDER, crit, "2026-10-07", "paper에서 live_1로 승격합니다", {T: 10_000}, path=db)
        ladder_ops.promote(LADDER, crit, "2026-10-07", "paper에서 live_1로 승격합니다", {T: 10_000},
                           account_value=1_500_000, path=db)
        assert store.sleeve_cash("real", db) == 1_500_000  # min(계좌 150만, 한도 200만)

    def test_promotion_report_on_shipped_config_says_not_eligible(self, db, capsys, monkeypatch):
        from executor import metrics
        monkeypatch.setattr(metrics, "baseline_benchmark", lambda *a, **k: None)
        assert promotion_report.run(db_path=db) == 0
        out = capsys.readouterr().out
        assert "승격 불가" in out and "미확정" in out


# ---------------------------------------------------------------- 실행 안정성

class TestExecutionRobustness:
    def test_same_day_rerun_does_not_resend(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000}, reject_tickers={T})
        _run(b, "2026-10-01", 10_000, db=db)
        b.reject_tickers.clear()
        r = _run(b, "2026-10-01", 10_000, db=db)  # 재시작해서 같은 날 다시 실행
        assert r.orders_skipped_duplicate == 1 and len(_buys(b)) == 1

    def test_partial_fill_is_booked_as_filled_qty(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000}, partial_ratio={T: 0.5})
        r = _run(b, "2026-10-01", 10_000, db=db)
        assert r.status == "ok"  # 원장과 브로커가 체결분만큼 일치 → 대사 통과
        assert store.internal_positions("shadow", db)[T] == b.positions[T] == 99
        assert "partial_fill" in set(store.events(path=db)["kind"])

    def test_fill_delay_within_polls_fills_and_beyond_is_cancelled(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000}, fill_delay_polls=2)
        _run(b, "2026-10-01", 10_000, db=db)
        assert b.positions[T] == 199
        b2 = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000}, fill_delay_polls=99)
        p2 = db.parent / "e2.db"
        store.init_db(p2)
        r = engine.run_once(b2, LADDER, "2026-10-01", {T: 1.0}, {T: 10_000}, path=p2, fill="same_close")
        assert r.status == "ok" and b2.positions.get(T, 0) == 0
        assert store.orders(path=p2).iloc[0]["status"] == "cancelled"

    def test_tier_history_is_append_only(self, db):
        _enter_live_1(db)
        before = store.tier_history(db).to_dict("records")
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        _run(b, "2026-11-02", 10_000, db=db)
        _run(b, "2026-11-03", 8_000, db=db)
        after = store.tier_history(db).to_dict("records")
        assert after[: len(before)] == before and len(after) == len(before) + 1


# ---------------------------------------------------------------- 4. 적립 기록

class TestContributions:
    def test_record_and_validate(self, db, capsys):
        assert add_contribution.main(["contribution", "--date", "2026-10-25", "--amount", "1000000",
                                      "--to", "savings"], db_path=db) == 0
        add_contribution.main(["contribution", "--date", "2026-10-25", "--amount", "200000", "--to", "trading"],
                              db_path=db)
        add_contribution.main(["list"], db_path=db)
        assert "적립 계좌 평가금액: 데이터 없음" in capsys.readouterr().out
        with pytest.raises(SystemExit):
            add_contribution.main(["contribution", "--date", "x", "--amount", "1", "--to", "elsewhere"], db_path=db)
        with pytest.raises(ValueError):
            store.add_contribution("2026-10-25", -5, "savings", path=db)
        assert len(store.contributions(db)) == 2

    def test_trading_contribution_does_not_raise_cap(self, db):
        _enter_live_1(db)
        store.add_contribution("2026-11-02", 5_000_000, "trading", path=db)
        b = FakeBroker("real", cash=11_000_000, prices={T: 10_000})
        assert _run(b, "2026-11-02", 10_000, db=db).planning_capital == 2_000_000


# ---------------------------------------------------------------- 5. 자금 배분 화면

LEAK = re.compile(r"\b(nan|NaN|None|inf|null)\b")


def _visible(html: str) -> str:
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class TestAllocationPage:
    def test_context_and_render_have_all_parts_and_no_leaks(self, db, monkeypatch):
        from executor import metrics, web
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        _run(b, "2026-11-02", 10_000, db=db)
        _run(b, "2026-11-03", 8_000, db=db)  # 강등 1건
        store.add_contribution("2026-10-25", 1_000_000, "savings", path=db)
        bench = pd.Series([10_000.0, 8_500.0], index=pd.to_datetime(["2026-11-02", "2026-11-03"]))
        ctx = build_allocation_context(LADDER, {"stages": {"p2l": EASY}}, bench, db)
        assert ctx["current"]["tier"] == "paper" and ctx["savings"]["valuation"] == "데이터 없음"
        assert ctx["demotions"] and "MDD" in ctx["demotions"][0]["reason"]
        live = next(s for s in ctx["stints"] if s["tier"] == "live_1")
        assert live["days"] == 2 and live["trades"] >= 1 and live["bh_cum"] == "-15.00%"

        from fastapi.testclient import TestClient
        monkeypatch.setattr(metrics, "baseline_benchmark", lambda *a, **k: bench)
        text = _visible(TestClient(web.app).get("/").text)
        for needle in ("자금 배분 현황", "현재 단계", "운용 한도", "한도 밖 현금", "적립 계좌 누적 납입액",
                       "단계별 성과", "다음 승격 조건", "최근 강등 이력", "운용 일수"):
            assert needle in text, needle
        assert not LEAK.search(text), LEAK.search(text)

    def test_page_with_no_db_shows_no_data(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from executor import web
        monkeypatch.setattr(store, "DB_PATH", tmp_path / "missing.db")
        r = TestClient(web.app).get("/")
        assert r.status_code == 200 and "데이터 없음" in _visible(r.text)
        assert not (tmp_path / "missing.db").exists()  # 화면은 DB를 만들지도 않는다


# ---------------------------------------------------------------- 2026-10-18 결정: cap_rejected 연속 N회·비교 종목

class TestCapRejectedStreak:
    def test_single_cap_rejected_is_not_an_order_error_but_n_in_a_row_is(self, db, monkeypatch):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        bad = lambda *a, **k: Plan(capital=2_000_000, orders=[PlannedOrder(T, "buy", 300, 10_000.0)])  # noqa: E731
        monkeypatch.setattr(engine, "plan_orders", bad)
        for i in range(2):
            assert _run(b, f"2026-11-0{2 + i}", 10_000, db=db, cap_rejected_streak=3).status == "cap_rejected"
        assert "order_error" not in set(store.events(path=db)["kind"])
        assert store.current_tier(db)["to_tier"] == "live_1"  # 1·2회째는 강등 사유 아님
        _run(b, "2026-11-04", 10_000, db=db, cap_rejected_streak=3)
        errs = store.events(path=db)
        assert (errs["kind"] == "order_error").sum() == 1 and "연속 3회" in errs[errs["kind"] == "order_error"].iloc[0]["message"]
        assert store.current_tier(db)["to_tier"] == "paper" and store.freeze_state("real", db) == "review_required"

    def test_streak_resets_after_a_normal_run(self, db, monkeypatch):
        _enter_live_1(db)
        b = FakeBroker("real", cash=6_000_000, prices={T: 10_000})
        bad = lambda *a, **k: Plan(capital=2_000_000, orders=[PlannedOrder(T, "buy", 300, 10_000.0)])  # noqa: E731
        from executor import planner
        for i, fn in enumerate([bad, bad, planner.plan_orders, bad, bad]):
            monkeypatch.setattr(engine, "plan_orders", fn)
            _run(b, f"2026-11-{2 + i:02d}", 10_000, db=db, cap_rejected_streak=3)
        assert "order_error" not in set(store.events(path=db)["kind"])


class TestBenchmarks:
    def test_mdd_criterion_is_checked_against_every_benchmark_and_missing_data_fails(self):
        from executor.criteria import evaluate_items
        from executor.tiers import TierMetrics
        m = TierMetrics("2026-01-01", "2026-06-01", 100, 10, 1.0, -8.0, 0.0, 0, 0,
                        benchmarks={"000660": {"return_pct": 5.0, "mdd_pct": -20.0},
                                    "069500": {"return_pct": None, "mdd_pct": None}})
        items = evaluate_items({"max_mdd_ratio_vs_buy_hold": 1.0}, m)
        assert [(i.label, i.passed) for i in items] == [
            ("MDD / 단순 보유 MDD (000660)", True), ("MDD / 단순 보유 MDD (069500)", False)]
        assert "데이터 없음" in items[1].note

    def test_shipped_executor_config_has_both_benchmarks_and_collector_knows_kodex200(self):
        import yaml
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        cfg = yaml.safe_load((root / "config/executor.yaml").read_text())
        assert cfg["benchmarks"] == ["000660", "069500"] and cfg["cap_rejected_streak"] == 3
        assert '"069500.KS": "069500"' in (root / "collectors/kr_market.py").read_text()
