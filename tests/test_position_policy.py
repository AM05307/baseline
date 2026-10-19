"""
포지션 정책(analysis/position_policy) — 백테스트(what_if)와 실행(planner·engine)이 같은 규칙을 쓰는지.

docs/reference_gap_analysis.md 7절의 문제: what_if는 목표가 바뀔 때만 거래하고 planner는 매일 드리프트를 맞춰서,
섀도 괴리(자동 강등 기준)에 구현 차이가 섞였다. 여기서는 (1) 정책 자체, (2) planner가 드리프트로 거래하지 않는지,
(3) 엔진이 체결 완료 목표만 기록하는지, (4) 정수 주식 백테스트(what_if.simulate_shares)와 엔진+FakeBroker의
수량 경로·자산이 **정확히** 같은지를 본다. 네트워크·실제 주문 없음.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import what_if as wi  # noqa: E402
from analysis.position_policy import PositionPolicy, ideal_quantity, target_quantity, weight_changed  # noqa: E402
from executor import engine, store  # noqa: E402
from executor.broker.fake import FakeBroker  # noqa: E402
from executor.planner import plan_orders  # noqa: E402
from executor.tiers import parse_ladder  # noqa: E402

T = "000660"
LADDER = parse_ladder({"tiers": [{"name": "shadow", "kind": "virtual", "max_capital_krw": 2_000_000, "confirmed": True}]})
P = PositionPolicy()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "executor.db"
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init_db(p)
    return p


class TestPolicy:
    def test_zero_target_is_always_flat(self):
        assert target_quantity(7, 0.0, 0.0, 1_000, 10_000, P) == 0

    def test_same_target_keeps_quantity_despite_price_move(self):
        assert target_quantity(66, 0.66, 0.66, 1_050_000, 100_000_000, P) == 66

    def test_daily_mode_recomputes_on_drift(self):
        daily = PositionPolicy(rebalance="daily")
        assert target_quantity(66, 0.66, 0.66, 1_050_000, 103_300_000, daily) == 64

    def test_missing_last_target_recomputes(self):
        assert weight_changed(0.66, None)
        assert target_quantity(0, 0.66, None, 1_000_000, 100_000_000, P) == 66

    def test_changed_target_recomputes(self):
        assert target_quantity(100, 0.33, 1.0, 1_000_000, 100_000_000, P) == 33

    def test_holding_above_capital_forces_recompute(self):
        # 손실·강등으로 자본이 줄어 지금 보유가 자본을 넘으면 같은 목표여도 다시 계산(한도 검사가 막히지 않게)
        assert target_quantity(10, 1.0, 1.0, 1_000, 5_000, P) == 5

    def test_buy_cost_keeps_full_weight_inside_capital(self):
        q = ideal_quantity(1.0, 1_000, 100_000, PositionPolicy(buy_cost_rate=0.001))
        assert q == 99 and q * 1_000 * 1.001 <= 100_000

    def test_lot_size(self):
        assert ideal_quantity(0.5, 1_000, 100_000, PositionPolicy(lot_size=0)) == pytest.approx(50.0)
        assert ideal_quantity(0.57, 1_000, 100_000, PositionPolicy(lot_size=10)) == 50

    def test_buffer_holds_small_changes_only(self):
        buf = PositionPolicy(rebalance="daily", buffer=0.10)
        assert target_quantity(95, 1.0, 1.0, 1_000, 100_000, buf) == 95   # |95-100| <= 10
        assert target_quantity(85, 1.0, 1.0, 1_000, 100_000, buf) == 100  # 버퍼 밖

    @pytest.mark.parametrize("kw", [{"rebalance": "weekly"}, {"lot_size": -1}, {"buffer": -0.1}])
    def test_invalid_policy(self, kw):
        with pytest.raises(ValueError):
            PositionPolicy(**kw)

    def test_invalid_weight_and_price(self):
        with pytest.raises(ValueError):
            ideal_quantity(1.5, 1_000, 1_000, P)
        with pytest.raises(ValueError):
            ideal_quantity(0.5, 0, 1_000, P)


class TestPlannerNoDriftTrading:
    def test_same_target_with_last_target_sends_nothing(self):
        # 분석 문서 scratch 계산과 같은 상황: 66% 고정, 가격 +5% → 예전 planner는 매도 2주
        plan = plan_orders({T: 0.66}, {T: 1_050_000}, {T: 66}, 103_300_000, last_targets={T: 0.66})
        assert plan.orders == []

    def test_daily_policy_reproduces_old_behaviour(self):
        plan = plan_orders({T: 0.66}, {T: 1_050_000}, {T: 66}, 103_300_000, last_targets={T: 0.66},
                           policy=PositionPolicy(rebalance="daily"))
        assert [(o.side, o.qty) for o in plan.orders] == [("sell", 2)]

    def test_fractional_policy_rejected_for_orders(self):
        with pytest.raises(ValueError):
            plan_orders({T: 0.5}, {T: 1_000}, {}, 10_000, policy=PositionPolicy(lot_size=0))


def _engine_run(broker, db, date, weight, price):
    broker.prices[T] = price
    return engine.run_once(broker, LADDER, date, {T: weight}, {T: price}, path=db,
                           policy=PositionPolicy(buy_cost_rate=engine.DEFAULT_COMMISSION),
                           kill_switch_file=db.parent / "KILL_SWITCH", fill="same_close")


class TestEngineTargetHistory:
    def test_complete_fill_records_target_and_next_day_holds(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        assert _engine_run(b, db, "2026-10-12", 0.66, 10_000).orders_sent == 1
        assert store.last_targets("shadow", db) == {T: 0.66}
        assert _engine_run(b, db, "2026-10-13", 0.66, 11_000).orders_sent == 0  # 드리프트는 맞추지 않음

    def test_partial_fill_is_not_recorded_so_next_day_recomputes(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000}, partial_ratio={T: 0.5})
        _engine_run(b, db, "2026-10-12", 1.0, 10_000)
        assert store.last_targets("shadow", db) == {}
        b.partial_ratio = {}
        r = _engine_run(b, db, "2026-10-13", 1.0, 10_000)
        assert r.orders_sent == 1 and store.last_targets("shadow", db) == {T: 1.0}
        assert b.positions[T] == store.internal_positions("shadow", db)[T]

    def test_rejected_order_is_not_recorded(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000}, reject_tickers={T})
        _engine_run(b, db, "2026-10-12", 1.0, 10_000)
        assert store.last_targets("shadow", db) == {}

    def test_target_history_is_append_only(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _engine_run(b, db, "2026-10-12", 1.0, 10_000)
        _engine_run(b, db, "2026-10-13", 0.33, 10_000)
        with store.conn(db) as c:
            rows = c.execute("SELECT decision_date, target_weight FROM target_history ORDER BY id").fetchall()
        assert [tuple(r) for r in rows] == [("2026-10-12", 1.0), ("2026-10-13", 0.33)]
        assert store.last_targets("shadow", db) == {T: 0.33}


class TestBacktestEngineEquivalence:
    """정수 주식 백테스트와 엔진+FakeBroker가 날짜별 수량·자산까지 정확히 같아야 한다(허용 오차 비교 아님)."""

    def test_quantity_path_and_equity_match_exactly(self, db):
        days = pd.bdate_range("2026-09-01", periods=30)
        rng = np.random.default_rng(7)
        close = pd.Series(np.round(10_000 * np.cumprod(1 + rng.normal(0, 0.03, len(days)))), index=days)
        weights = pd.Series([1.0] * 6 + [0.66] * 7 + [0.33] * 5 + [0.0] * 3 + [0.66] * 4 + [1.0] * 5, index=days)

        sim = wi.simulate_shares(close, weights, 2_000_000, PositionPolicy(buy_cost_rate=engine.DEFAULT_COMMISSION),
                                 engine.DEFAULT_COMMISSION, lambda d: engine.DEFAULT_TAX, fill="same_close")

        b = FakeBroker("shadow", cash=2_000_000, prices={T: float(close.iloc[0])},
                       commission_rate=engine.DEFAULT_COMMISSION, tax_rate=engine.DEFAULT_TAX)
        qty, equity = [], []
        for d in days:
            r = _engine_run(b, db, d.date().isoformat(), float(weights[d]), float(close[d]))
            assert r.status == "ok", r.message
            qty.append(store.internal_positions("shadow", db).get(T, 0))
            equity.append(store.snapshots("shadow", path=db).iloc[-1]["sleeve_value"])

        assert qty == [int(q) for q in sim["qty"]]
        assert equity == pytest.approx(list(sim["equity"]), abs=1e-6)
        # 거래한 날도 같다. 목표가 안 바뀐 날의 거래는 이익으로 보유 평가액이 한도(2백만)를 넘어 줄인 매도뿐이다
        assert int((sim["traded"] > 0).sum()) == len(store.orders("shadow", db))
        changed = weights.ne(weights.shift())
        held_before = sim["qty"].shift().fillna(0) * close
        drift = (sim["traded"] > 0) & ~changed
        assert (held_before[drift] > 2_000_000).all()
        assert (sim["qty"][drift] < sim["qty"].shift()[drift]).all()

    def test_fractional_simulate_unchanged_for_constant_target(self):
        idx = pd.bdate_range("2026-09-01", periods=3)
        ret = pd.Series([0.0, 0.1, -0.1], index=idx)
        costs = wi.CostModel(0.0, 0.0, lambda d: 0.0)
        sim = wi.simulate(ret, pd.Series(0.5, index=idx), pd.Series(idx, index=idx), 0.5, costs)
        assert not sim["changed"].any() and sim["traded"].sum() == 0
