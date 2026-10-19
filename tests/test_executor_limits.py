"""
운영 제한(2026-10-19, 기본값 미확정): 종목당 상한(지수 ETF 예외) · 일일 손실률 · 일일 주문 횟수 · 강등·정지 뒤 쿨다운.
planner의 순수 함수와 engine 배선을 FakeBroker로만 검증한다(네트워크·실제 주문 없음).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import config_files, engine, store  # noqa: E402
from executor.broker.fake import FakeBroker  # noqa: E402
from executor.planner import (DailyLimits, DailyState, Plan, PlannedOrder, apply_daily_limits,  # noqa: E402
                              cap_single_weights)
from executor.tiers import parse_ladder  # noqa: E402

T, ETF = "000660", "069500"
LADDER = parse_ladder({"tiers": [{"name": "shadow", "kind": "virtual", "max_capital_krw": 2_000_000, "confirmed": True}]})


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "e.db"
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init_db(p)
    return p


def _run(b, db, day, weights, prices, **kw):
    for t, p in prices.items():
        b.prices[t] = p
    return engine.run_once(b, LADDER, day, weights, prices, path=db, kill_switch_file=db.parent / "KS",
                           fill="same_close", **kw)


def _places(b):
    return [c for c in b.calls if c[0] == "place_order"]


class TestCapSingleWeights:
    def test_individual_stock_capped_etf_exempt(self):
        w, notes = cap_single_weights({T: 1.0, ETF: 0.5}, 0.30, {ETF, "SPY", "QQQ"})
        assert w == {T: 0.30, ETF: 0.5} and len(notes) == 1 and T in notes[0]

    def test_none_disables_and_bad_value_rejected(self):
        assert cap_single_weights({T: 1.0}, None) == ({T: 1.0}, [])
        with pytest.raises(ValueError):
            cap_single_weights({T: 1.0}, 1.5)

    def test_below_cap_untouched(self):
        assert cap_single_weights({T: 0.2}, 0.3) == ({T: 0.2}, [])


def _plan(*orders):
    return Plan(capital=1.0, orders=[PlannedOrder(t, s, q, 100.0) for t, s, q in orders])


class TestApplyDailyLimits:
    LIM = DailyLimits(max_daily_loss_pct=3.0, max_orders_per_day=2, cooldown_days=5)

    def test_order_count_over_limit_rejects_whole_plan(self):
        p = _plan((T, "sell", 1), (ETF, "buy", 1))
        reasons = apply_daily_limits(p, self.LIM, DailyState(orders_today=1))
        assert p.rejected and p.orders == [] and "주문 횟수" in reasons[0]

    def test_loss_blocks_buys_keeps_sells(self):
        p = _plan((T, "sell", 1), (ETF, "buy", 1))
        reasons = apply_daily_limits(p, self.LIM, DailyState(sleeve_change_pct=-3.0))
        assert not p.rejected and [o.side for o in p.orders] == ["sell"] and "일일 손실" in reasons[0]

    def test_cooldown_blocks_buys_until_elapsed(self):
        p = _plan((T, "buy", 1))
        assert apply_daily_limits(p, self.LIM, DailyState(days_since_freeze=4)) and p.orders == []
        p = _plan((T, "buy", 1))
        assert apply_daily_limits(p, self.LIM, DailyState(days_since_freeze=5)) == [] and len(p.orders) == 1

    def test_within_limits_changes_nothing(self):
        p = _plan((T, "buy", 1))
        assert apply_daily_limits(p, self.LIM, DailyState(sleeve_change_pct=-2.9, orders_today=1)) == []
        assert len(p.orders) == 1

    def test_from_config_zero_or_missing_disables(self):
        assert DailyLimits.from_config({"max_daily_loss_pct": 0, "max_orders_per_day": 0}) == DailyLimits()
        assert DailyLimits.from_config(None) == DailyLimits()

    def test_shipped_config_has_conservative_defaults(self):
        cfg = config_files.load_executor_config(record=False)
        lim = DailyLimits.from_config(cfg["daily_limits"])
        assert lim.max_daily_loss_pct and lim.max_orders_per_day and lim.cooldown_days > 0
        assert cfg["concentration"]["max_single_weight"] == 0.30
        assert {"069500", "SPY", "QQQ"} <= set(cfg["concentration"]["exempt"])


class TestEngineWiring:
    LIM = DailyLimits(max_daily_loss_pct=3.0, max_orders_per_day=4, cooldown_days=5)

    def test_daily_loss_blocks_buy_and_does_not_record_target(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _run(b, db, "2026-10-15", {T: 0.5}, {T: 10_000}, daily_limits=self.LIM)
        n = len(_places(b))
        r = _run(b, db, "2026-10-16", {T: 1.0}, {T: 8_000}, daily_limits=self.LIM)  # 운용분 약 −10%
        assert r.status == "ok" and len(_places(b)) == n  # 매수 보류
        assert store.last_targets("shadow", db) == {T: 0.5}  # 다음 날 다시 계산하게 기록 안 함
        assert "daily_limit" in set(store.events(path=db)["kind"])

    def test_order_count_limit_rejects_and_sends_nothing(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000, ETF: 10_000})
        r = _run(b, db, "2026-10-15", {T: 0.3, ETF: 0.3}, {T: 10_000, ETF: 10_000},
                 daily_limits=DailyLimits(max_orders_per_day=1))
        assert r.status == "daily_limit" and _places(b) == [] and store.orders(path=db).empty

    def test_rerun_same_day_does_not_count_its_own_orders(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        lim = DailyLimits(max_orders_per_day=1)
        _run(b, db, "2026-10-15", {T: 0.3}, {T: 10_000}, daily_limits=lim)
        r = _run(b, db, "2026-10-15", {T: 0.3}, {T: 10_000}, daily_limits=lim)
        assert r.status == "ok"

    def test_cooldown_after_freeze_blocks_buys_even_after_resume(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        store.append_event("2026-10-13", "shadow", "review_required", "test", path=db)
        store.append_event("2026-10-14", "shadow", "resume", "사람 확인", path=db)
        _run(b, db, "2026-10-15", {T: 0.3}, {T: 10_000}, daily_limits=self.LIM)
        assert _places(b) == []
        _run(b, db, "2026-10-19", {T: 0.3}, {T: 10_000}, daily_limits=self.LIM)  # 6일 경과
        assert len(_places(b)) == 1

    def test_single_weight_cap_in_engine_and_etf_exempt(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000, ETF: 10_000})
        r = _run(b, db, "2026-10-15", {T: 0.6, ETF: 0.4}, {T: 10_000, ETF: 10_000},
                 max_single_weight=0.3, weight_cap_exempt=frozenset({ETF}))
        assert r.status == "ok"
        pos = store.internal_positions("shadow", db)
        assert pos[T] * 10_000 <= 0.3 * 2_000_000 and pos[ETF] * 10_000 > 0.3 * 2_000_000
        assert store.last_targets("shadow", db) == {T: 0.3, ETF: 0.4}
        assert "concentration_cap" in set(store.events(path=db)["kind"])
