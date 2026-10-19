"""
체결 시점 next_open(t일 종가 결정 → t+1일 시가 체결) — 엔진 두 단계(run_once 결정 / execute_pending 체결)와
비율 백테스트(analysis/what_if.simulate_next_open). FakeBroker만 쓴다(네트워크·실제 주문 없음).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import what_if as wi  # noqa: E402
from executor import engine, store  # noqa: E402
from executor.broker.fake import FakeBroker  # noqa: E402
from executor.tiers import parse_ladder  # noqa: E402

T = "000660"
LADDER = parse_ladder({"tiers": [{"name": "shadow", "kind": "virtual", "max_capital_krw": 2_000_000, "confirmed": True}]})
NO_COST = wi.CostModel(0.0, 0.0, lambda d: 0.0)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "e.db"
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init_db(p)
    return p


def _decide(b, db, day, w, close, **kw):
    b.prices[T] = close
    return engine.run_once(b, LADDER, day, {T: w}, {T: close}, path=db, kill_switch_file=db.parent / "KS", **kw)


def _fill(b, db, day, open_, valid, **kw):
    b.prices[T] = open_
    return engine.execute_pending(b, LADDER, day, {T: open_}, valid_decision_dates=valid, path=db,
                                  kill_switch_file=db.parent / "KS", **kw)


def _places(b):
    return [c for c in b.calls if c[0] == "place_order"]


class TestEngineTwoPhase:
    def test_default_decision_sends_nothing_and_next_open_fills(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        r = _decide(b, db, "2026-10-15", 1.0, 10_000)
        assert r.status == "ok" and _places(b) == [] and store.orders(path=db).iloc[0]["status"] == "planned"
        assert store.last_targets("shadow", db) == {}  # 아직 체결 완료 목표 아님
        f = _fill(b, db, "2026-10-16", 10_100, {"2026-10-15"})
        assert f.orders_sent == 1 and len(_places(b)) == 1
        assert store.internal_positions("shadow", db)[T] == b.positions[T]
        assert store.last_targets("shadow", db) == {T: 1.0}

    def test_fill_runs_do_not_break_cap_rejected_streak(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _decide(b, db, "2026-10-15", 1.0, 10_000)
        _fill(b, db, "2026-10-16", 10_000, {"2026-10-15"})
        with store.conn(db) as c:
            n = c.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0]
        assert n == 1  # 결정 실행만 기록

    def test_kill_switch_or_freeze_blocks_fill_and_keeps_pending(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _decide(b, db, "2026-10-15", 1.0, 10_000)
        (db.parent / "KS").write_text("")
        assert _fill(b, db, "2026-10-16", 10_000, {"2026-10-15"}).status == "blocked_kill_switch"
        (db.parent / "KS").unlink()
        store.append_event("2026-10-16", "shadow", "halt", "test", path=db)
        assert _fill(b, db, "2026-10-16", 10_000, {"2026-10-15"}).status == "halt"
        assert _places(b) == [] and store.orders(path=db).iloc[0]["status"] == "planned"

    def test_same_close_option_sends_immediately(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _decide(b, db, "2026-10-15", 1.0, 10_000, fill="same_close")
        assert len(_places(b)) == 1

    def test_no_order_decision_is_complete_immediately(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _decide(b, db, "2026-10-15", 0.0, 10_000)
        assert store.last_targets("shadow", db) == {T: 0.0} and store.orders(path=db).empty

    def test_invalid_fill_mode_rejected(self, db):
        with pytest.raises(ValueError):
            _decide(FakeBroker("shadow", cash=1), db, "2026-10-15", 1.0, 10_000, fill="next_close")


class TestFractionalNextOpen:
    IDX = pd.bdate_range("2026-10-01", periods=5)

    def test_constant_full_weight_equals_close_to_close(self):
        close = pd.Series([100, 110, 99, 120, 130.0], index=self.IDX)
        open_ = pd.Series([100, 105, 104, 118, 125.0], index=self.IDX)
        sim = wi.simulate_next_open(close, open_, pd.Series(1.0, index=self.IDX), 1.0, NO_COST)
        assert list(sim.index) == list(self.IDX[1:])
        assert sim["equity"].iloc[-1] == pytest.approx(130 / 100)

    def test_switch_takes_effect_at_next_open_not_at_close(self):
        close = pd.Series([100, 100, 100, 100, 100.0], index=self.IDX)
        open_ = pd.Series([100, 100, 90, 100, 100.0], index=self.IDX)  # 3일째 시가 −10% 갭, 종가 회복
        decided = pd.Series([1.0, 0.0, 0.0, 0.0, 0.0], index=self.IDX)  # 2일째 종가에 0으로 결정
        sim = wi.simulate_next_open(close, open_, decided, 1.0, NO_COST)
        d3 = sim.loc[self.IDX[2]]
        assert d3["changed"] and d3["weight"] == 0.0
        assert d3["ret"] == pytest.approx(-0.10)  # 갭은 이전 비중(100%)으로 맞고, 시가 이후 회복은 못 받는다
        assert d3["gap_pct"] == pytest.approx(-10.0)

    def test_zero_weight_returns_zero(self):
        close = pd.Series([100, 120, 80, 90, 150.0], index=self.IDX)
        open_ = close.shift(1).fillna(100) * 1.01
        sim = wi.simulate_next_open(close, open_, pd.Series(0.0, index=self.IDX), 0.0, NO_COST)
        assert (sim["ret"] == 0).all()

    def test_no_lookahead_decision_after_t_does_not_change_t(self):
        rng = np.random.default_rng(1)
        idx = pd.bdate_range("2026-01-01", periods=40)
        close = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.02, 40)), index=idx)
        open_ = close.shift(1).fillna(100) * (1 + rng.normal(0, 0.01, 40))
        decided = pd.Series(rng.choice([1.0, 0.66, 0.33], 40), index=idx)
        base = wi.simulate_next_open(close, open_, decided, 1.0, NO_COST)
        d2 = decided.copy()
        d2[idx > idx[20]] = 0.0  # t=20 이후 결정만 바꿈
        alt = wi.simulate_next_open(close, open_, d2, 1.0, NO_COST)
        pd.testing.assert_frame_equal(base.loc[:idx[21]], alt.loc[:idx[21]])  # t+1일(시가 체결)까지 동일


class TestWeightGapWarning:
    """주문 단위(정수 주식) 때문에 목표 비중을 못 맞추면 경고(주문은 막지 않음)."""

    def test_000660_like_price_warns_for_100_and_33_but_not_66(self):
        from analysis.position_policy import PositionPolicy
        from executor.planner import weight_gap_warnings
        pol = PositionPolicy(buy_cost_rate=engine.DEFAULT_COMMISSION)
        assert weight_gap_warnings({T: 1.0}, {T: 1_272_000}, 2_000_000, pol)  # 1주 = 63.6%
        assert weight_gap_warnings({T: 0.33}, {T: 1_272_000}, 2_000_000, pol)  # 0주 = 0%
        assert weight_gap_warnings({T: 0.66}, {T: 1_272_000}, 2_000_000, pol) == []  # 1주 = 63.6%, 차이 2.4%p
        assert weight_gap_warnings({T: 1.0}, {T: 40_000}, 2_000_000, pol) == []  # ETF 가격대면 1~3%p

    def test_threshold_is_configurable_and_zero_weight_never_warns(self):
        from analysis.position_policy import PositionPolicy
        from executor.planner import weight_gap_warnings
        pol = PositionPolicy()
        assert weight_gap_warnings({T: 1.0}, {T: 70_000}, 2_000_000, pol, max_gap_pctp=1.0)  # 28주 = 98%
        assert weight_gap_warnings({T: 0.0}, {T: 1_272_000}, 2_000_000, pol) == []

    def test_engine_records_warning_event_but_still_plans(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 1_272_000})
        r = _decide(b, db, "2026-10-15", 1.0, 1_272_000)
        assert r.status == "ok" and r.warnings and len(r.planned) == 1
        assert "weight_gap_warning" in set(store.events(path=db)["kind"])


def test_lot_size_table_script_marks_missing_price_as_no_data(capsys, monkeypatch, tmp_path):
    from db import store as bstore
    from scripts import lot_size_table
    monkeypatch.setattr(bstore, "DB_PATH", tmp_path / "b.db")
    bstore.init_db(tmp_path / "b.db")
    assert lot_size_table.main(["--tickers", "069500,SPY", "--price", "069500=40000", "--capital", "2000000"]) == 0
    out = capsys.readouterr().out
    assert "49주" in out and "직접 입력(가정)" in out
    assert "SPY: 가격 데이터 없음" in out or "SPY: 환율 데이터 없음" in out
