"""
재시작 복구(executor/recovery.py) — FakeBroker만 쓴다(네트워크·실제 주문 없음).

"프로세스 종료"는 BaseException(Crash)으로 흉내 낸다: 엔진은 이것을 잡지 않으므로 DB에는 죽기 직전까지 쓴 것만 남는다.
브로커 객체는 그대로 다시 쓴다 — 재시작해도 증권사 쪽 주문·잔고는 남아 있기 때문이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import engine, recovery, store  # noqa: E402
from executor.broker.base import BrokerError  # noqa: E402
from executor.broker.fake import FakeBroker  # noqa: E402
from executor.tiers import parse_ladder  # noqa: E402

T = "000660"
LADDER = parse_ladder({"tiers": [{"name": "shadow", "kind": "virtual", "max_capital_krw": 2_000_000, "confirmed": True}]})
D1, D2 = "2026-10-15", "2026-10-16"


class Crash(BaseException):
    """프로세스가 죽은 것으로 친다."""


class CrashingBroker(FakeBroker):
    """crash_after_place=True면 주문이 브로커에 들어간 직후, crash_before_cancel=True면 잔량 취소 직전에 죽는다."""

    crash_after_place = False
    crash_before_cancel = False

    def place_order(self, *a, **kw):
        st = super().place_order(*a, **kw)
        if self.crash_after_place:
            raise Crash("전송 직후 종료")
        return st

    def cancel_order(self, oid):
        if self.crash_before_cancel:
            raise Crash("부분 체결 뒤 취소 전에 종료")
        return super().cancel_order(oid)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "e.db"
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init_db(p)
    return p


def _decide(b, db, w=1.0, price=10_000):
    b.prices[T] = price
    return engine.run_once(b, LADDER, D1, {T: w}, {T: price}, path=db, kill_switch_file=db.parent / "KS")


def _fill(b, db, price=10_000):
    b.prices[T] = price
    return engine.execute_pending(b, LADDER, D2, {T: price}, valid_decision_dates={D1}, path=db,
                                  kill_switch_file=db.parent / "KS")


def _places(b):
    return [c for c in b.calls if c[0] == "place_order"]


def _kinds(db):
    return list(store.events(path=db)["kind"])


class TestCrashRightAfterSend:
    def test_fill_confirmed_by_broker_is_recorded_once_and_not_resent(self, db):
        b = CrashingBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _decide(b, db)
        b.crash_after_place = True
        with pytest.raises(Crash):
            _fill(b, db)
        row = store.orders(path=db).iloc[0]
        assert row["status"] == "sending" and row["broker_order_id"] is None  # 전송 직전 기록만 남음
        assert store.internal_positions("shadow", db) == {} and b.positions[T] > 0  # 브로커에선 체결됨

        b.crash_after_place = False  # 재시작
        r = _fill(b, db)
        assert r.status == "ok" and len(_places(b)) == 1  # 다시 보내지 않음
        row = store.orders(path=db).iloc[0]
        assert row["status"] == "filled" and row["broker_order_id"] == "F1"
        assert store.internal_positions("shadow", db) == {T: b.positions[T]}
        assert store.last_targets("shadow", db) == {T: 1.0}
        assert "recovery_auto" in _kinds(db) and store.freeze_state("shadow", db) is None

    def test_submitted_but_still_open_halts_then_resolves_after_broker_fills(self, db):
        b = CrashingBroker("shadow", cash=2_000_000, prices={T: 10_000}, fill_delay_polls=5)
        _decide(b, db)
        b.crash_before_cancel = True  # 폴링 3회 동안 체결 안 됨 → 취소하려다 죽음
        with pytest.raises(Crash):
            _fill(b, db)
        assert store.orders(path=db).iloc[0]["status"] == "submitted"

        b.crash_before_cancel = False
        r = _fill(b, db)
        assert r.status == "halt" and store.freeze_state("shadow", db) == "halt"
        assert "recovery_manual" in _kinds(db) and "미체결 잔량" in r.message
        assert len(_places(b)) == 1 and store.internal_positions("shadow", db) == {}

        for _ in range(3):  # 거래소에서 결국 체결됨(시간 경과 — fill_delay_polls=5를 넘김)
            b.get_order_status("F1")
        store.append_event(D2, "shadow", "resume", "사람이 확인", path=db)
        r = _fill(b, db)
        assert r.status == "ok" and store.orders(path=db).iloc[0]["status"] == "filled"
        assert store.internal_positions("shadow", db) == {T: b.positions[T]}


class TestCrashDuringPartialFill:
    def test_partial_fill_recorded_open_remainder_halts_then_cancel_confirmed_resolves(self, db):
        b = CrashingBroker("shadow", cash=2_000_000, prices={T: 10_000}, partial_ratio={T: 0.5})
        _decide(b, db)
        planned = int(store.orders(path=db).iloc[0]["qty"])
        b.crash_before_cancel = True
        with pytest.raises(Crash):
            _fill(b, db)
        assert store.orders(path=db).iloc[0]["status"] == "submitted"
        assert store.internal_positions("shadow", db) == {}  # 원장엔 아직 체결 없음

        b.crash_before_cancel = False  # 재시작
        r = _fill(b, db)
        assert r.status == "halt" and "미체결 잔량" in r.message
        half = planned // 2
        assert store.internal_positions("shadow", db) == {T: half} == {T: b.positions[T]}  # 확인된 체결분은 반영
        assert len(_places(b)) == 1

        b.cancel_order("F1")  # 사람이 증권사에서 잔량 취소
        store.append_event(D2, "shadow", "resume", "잔량 취소 확인", path=db)
        r = _fill(b, db)
        assert r.status == "ok"
        row = store.orders(path=db).iloc[0]
        assert (row["status"], int(row["filled_qty"])) == ("partial", half)
        assert store.internal_positions("shadow", db) == {T: half}  # 이중 기록 없음
        assert store.last_targets("shadow", db) == {}  # 부분 체결 → 체결 완료 목표로 기록하지 않음
        assert len(_places(b)) == 1


class TestAmbiguousCasesHalt:
    def test_sending_row_without_any_broker_order_halts(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _decide(b, db)
        store.update_order(store.orders(path=db).iloc[0]["order_key"], status="sending", path=db)
        r = _fill(b, db)
        assert r.status == "halt" and "0건" in r.message and _places(b) == []

    def test_two_candidates_halt(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000})
        _decide(b, db)
        store.update_order(store.orders(path=db).iloc[0]["order_key"], status="sending", path=db)
        b.place_order(T, "buy", 1, 10_000), b.place_order(T, "buy", 1, 10_000)  # 같은 내용의 브로커 주문 둘
        r = _fill(b, db)
        assert r.status == "halt" and "2건" in r.message

    def test_broker_query_failure_halts(self, db):
        b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000}, fail_calls={"list_orders": 1})
        _decide(b, db)
        store.update_order(store.orders(path=db).iloc[0]["order_key"], status="sending", path=db)
        r = _fill(b, db)
        assert r.status == "halt" and "조회 실패" in r.message


def test_shadow_ledger_broker_marks_untraced_send_as_not_sent(db):
    class LedgerBroker(FakeBroker):
        ledger_is_broker = True

    b = LedgerBroker("shadow", cash=2_000_000, prices={T: 10_000})
    _decide(b, db)
    key = store.orders(path=db).iloc[0]["order_key"]
    store.update_order(key, status="sending", path=db)
    res = recovery.recover_on_start(b, "shadow", D2, commission=0.0, tax=0.0, path=db)
    assert not res.needs_manual and store.get_order(key, db)["status"] == "not_sent"


def test_same_close_mode_recovers_on_next_decision(db):
    b = CrashingBroker("shadow", cash=2_000_000, prices={T: 10_000})
    b.crash_after_place = True
    with pytest.raises(Crash):
        engine.run_once(b, LADDER, D1, {T: 1.0}, {T: 10_000}, path=db, kill_switch_file=db.parent / "KS",
                        fill="same_close")
    b.crash_after_place = False
    r = engine.run_once(b, LADDER, D2, {T: 1.0}, {T: 10_000}, path=db, kill_switch_file=db.parent / "KS",
                        fill="same_close")
    assert r.status == "ok" and store.internal_positions("shadow", db) == {T: b.positions[T]}
    assert len(_places(b)) == 1  # 목표 변화 없음 — 다시 사지 않음


def test_recovery_never_sends_or_cancels(db):
    b = FakeBroker("shadow", cash=2_000_000, prices={T: 10_000}, fill_delay_polls=99)
    _decide(b, db)
    key = store.orders(path=db).iloc[0]["order_key"]
    st = b.place_order(T, "buy", 1, 10_000)
    store.update_order(key, status="submitted", broker_order_id=st.broker_order_id, path=db)
    before = list(b.calls)
    recovery.recover_on_start(b, "shadow", D2, commission=0.0, tax=0.0, path=db)
    assert [c[0] for c in b.calls[len(before):]] == ["list_orders"]


def test_list_orders_error_type_is_broker_error():
    b = FakeBroker("shadow", fail_calls={"list_orders": 1})
    with pytest.raises(BrokerError):
        b.list_orders(D1)
