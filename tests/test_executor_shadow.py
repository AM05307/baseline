"""
섀도 실행기 end-to-end(FakeBroker 계열 ShadowBroker, 네트워크·실제 주문 없음) + 휴장일·데이터 신선도·킬 스위치.

baseline.db 대신 합성 BaselineInputs를 넣는다. 결정 규칙은 baseline과 같다: 결정일 정렬 → weight_history →
분리 방식 위험 점수 → 단계 → 비중.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import engine, shadow, store  # noqa: E402
from executor.broker.fake import FakeBroker  # noqa: E402
from executor.krx_calendar import parse_calendar  # noqa: E402
from executor.scripts import kill_switch as ks_cli  # noqa: E402
from executor.tiers import parse_ladder  # noqa: E402

T = "000660"
KST = shadow.KST
LADDER = parse_ladder({"tiers": [
    {"name": "shadow", "kind": "virtual", "max_capital_krw": 2_000_000, "confirmed": True},
    {"name": "paper", "kind": "virtual", "max_capital_krw": 2_000_000, "confirmed": True},
]})
CAL = parse_calendar({"covered_from": "2026-01-01", "covered_to": "2026-12-31", "holidays": [
    {"date": "2026-10-05", "name": "개천절 대체공휴일"}, {"date": "2026-10-09", "name": "한글날"}]})
CFG = {"benchmarks": [T], "cap_rejected_streak": 3, "kill_switch_file": "KILL_SWITCH",
       "freshness": {"max_foreign_signal_age_days": 4},
       # 이 파일의 합성 입력은 2026-06부터라 그룹 방식 기준 기간(2005~2018)이 없다 → 예전 가중치 방식으로 고정해 엔진·체결 흐름을
       # 검증한다. 그룹 방식(기본값)은 아래 test_group_method_* 가 따로 본다.
       "shadow": {"strategy": "risk_score", "ticker": T, "stage_weights": [1.0, 0.66, 0.33, 0.0], "combo_stage": 3,
                  "score_method": "weighted"}}


def _vals(dates, on):
    return pd.DataFrame({"date": pd.DatetimeIndex(dates), "value": np.asarray(on, float), "is_on": np.asarray(on, int)})


def _opens(close: pd.Series) -> pd.Series:
    """시가 = 전일 종가 ± 갭(위아래로 번갈아, 최대 ±1.5%). 첫날은 종가."""
    gap = 0.015 * np.cos(np.arange(len(close)) * 1.3)
    return (close.shift(1).fillna(close.iloc[0]) * (1 + gap)).round()


def _inputs(end="2026-10-16", risk_on_last=False, krx_signal_end=None, us_signal_end=None):
    """KRX 거래일(캘린더 반영) 시세 + 한국 장 신호 1개 + 해외 세션 신호 1개 + 가중치 기록 1건."""
    krx = pd.DatetimeIndex([d for d in pd.bdate_range("2026-06-01", end) if CAL.is_trading_day(d.date())])
    close = pd.Series(10_000 + np.arange(len(krx)) * 20.0, index=krx)
    kd = krx[krx <= pd.Timestamp(krx_signal_end)] if krx_signal_end else krx
    on = np.zeros(len(kd), int)
    if risk_on_last:
        on[-1] = 1
    us = pd.bdate_range("2026-06-01", us_signal_end or end)  # 미국 날짜(한국 휴장일에도 값 있음)
    opens = _opens(close)
    return shadow.BaselineInputs(
        close=close, open=opens, signal_ids=["KHJ_OVERHEAT", "YJH_US10Y"],
        directions={"KHJ_OVERHEAT": "risk", "YJH_US10Y": "risk"},
        values={"KHJ_OVERHEAT": _vals(kd, on), "YJH_US10Y": _vals(us, np.zeros(len(us), int))},
        weight_history=pd.DataFrame([{"id": 1, "recomputed_on": "2026-06-30", "signal_id": s, "new_weight_pct": 40.0,
                                      "algorithm_version": 3} for s in ("KHJ_OVERHEAT", "YJH_US10Y")]),
    )


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "executor.db"
    monkeypatch.setattr(store, "DB_PATH", p)
    store.init_db(p)
    return p


def _run(db, day, inputs=None, hour=16, ks_file=None, **kw):
    d = pd.Timestamp(day).date()
    return shadow.run_shadow(d, datetime(d.year, d.month, d.day, hour, 0, tzinfo=KST), ladder=LADDER, calendar=CAL,
                             cfg=CFG, inputs=inputs or _inputs(end=day), benchmark_close={T: None},
                             kill_switch_file=ks_file or (db.parent / "KILL_SWITCH"), path=db, **kw)


class TestEndToEnd:
    """기본 체결 시점: t일 종가로 결정 → 다음 거래일 시가에 체결(2026-10-19 사용자 결정)."""

    def test_decides_today_and_fills_at_next_open(self, db):
        inp = _inputs(end="2026-10-16")
        out = _run(db, "2026-10-15", inputs=inp)
        assert out.status == "ok" and out.stage == 0 and out.target_weight == 1.0
        planned = store.orders_shadow(db)
        assert len(planned) == 1 and planned.iloc[0]["side"] == "buy"
        assert store.internal_positions("shadow", db) == {}  # 아직 체결 안 됨 — 다음 거래일 시가 대기
        assert store.orders("shadow", db).iloc[0]["status"] == "planned"
        snap = store.snapshots("shadow", path=db).iloc[-1]
        assert snap["sleeve_value"] == snap["reference_value"] == 2_000_000

        out2 = _run(db, "2026-10-16", inputs=inp)
        assert out2.fill_result.orders_sent == 1
        filled = store.orders("shadow", db).iloc[0]
        assert filled["status"] == "filled" and store.internal_positions("shadow", db)[T] == filled["filled_qty"]
        led = sqlite3.connect(db).execute("SELECT price FROM sleeve_ledger WHERE kind='buy'").fetchone()
        assert led[0] == inp.open[pd.Timestamp("2026-10-16")]  # 시가로 체결
        gaps = store.shadow_gaps(db)
        assert list(gaps["decision_date"]) == ["2026-10-14", "2026-10-15"]  # 실행할 때마다(첫날 포함) 기록
        assert gaps.iloc[-1]["fill_date"] == "2026-10-16"
        assert gaps.iloc[-1]["gap_pct"] == pytest.approx(
            (inp.open[pd.Timestamp("2026-10-16")] / inp.close[pd.Timestamp("2026-10-15")] - 1) * 100)
        s = store.snapshots("shadow", path=db).iloc[-1]
        assert s["sleeve_value"] == pytest.approx(s["reference_value"], abs=1e-6)

    def test_same_close_option_still_fills_same_day(self, db):
        d = pd.Timestamp("2026-10-16").date()
        out = shadow.run_shadow(d, datetime(2026, 10, 16, 16, 0, tzinfo=KST), ladder=LADDER, calendar=CAL,
                                cfg={**CFG, "fill": "same_close"}, inputs=_inputs(end="2026-10-16"),
                                benchmark_close={T: None}, kill_switch_file=db.parent / "KS", path=db)
        assert out.status == "ok" and out.fill_result is None
        assert store.internal_positions("shadow", db)[T] == store.orders_shadow(db).iloc[0]["qty"]
        assert store.shadow_gaps(db).empty
        snap = store.snapshots("shadow", path=db).iloc[-1]
        assert snap["reference_value"] == pytest.approx(snap["sleeve_value"], abs=1e-6)

    def test_same_day_rerun_records_order_once(self, db):
        _run(db, "2026-10-16")
        _run(db, "2026-10-16")
        assert len(store.orders_shadow(db)) == 1
        assert len(store.shadow_runs(db)) == 2  # 시도는 둘 다 남는다
        assert len(store.orders("shadow", db)) == 1

    def test_stage_change_next_day_produces_sell_at_stage_weight(self, db):
        _run(db, "2026-10-15")
        out = _run(db, "2026-10-16", inputs=_inputs(end="2026-10-16", risk_on_last=True))  # 위험 40% → 70점 → 1단계
        assert out.stage == 1 and out.target_weight == 0.66
        sells = store.orders_shadow(db)
        assert list(sells.loc[sells["decision_date"] == "2026-10-16", "side"]) == ["sell"]

    def test_gap_is_recorded_every_day_even_without_orders(self, db):
        inp = _inputs(end="2026-10-16")
        for d in ("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"):
            _run(db, d, inputs=inp)
        assert list(store.shadow_gaps(db)["decision_date"]) == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]
        assert len(store.orders_shadow(db)) <= 2  # 주문은 첫 결정(과 한도 초과 매도)뿐인데 갭은 매일

    def test_skipped_day_makes_old_decision_expire(self, db):
        inp = _inputs(end="2026-10-16")
        _run(db, "2026-10-14", inputs=inp)  # 결정
        out = _run(db, "2026-10-16", inputs=inp)  # 10/15 실행을 빠뜨림 → 10/14 결정은 오늘 시가로 보내지 않음
        assert store.orders("shadow", db).set_index("decision_date").loc["2026-10-14", "status"] == "expired"
        assert "order_expired" in set(store.events(path=db)["kind"])
        assert out.fill_result.orders_sent == 0 and store.internal_positions("shadow", db) == {}
        assert out.status == "ok"  # 오늘 다시 결정한다

    def test_gap_up_buys_only_what_cash_allows(self, db):
        inp = _inputs(end="2026-10-16")
        inp.open[pd.Timestamp("2026-10-16")] = inp.close[pd.Timestamp("2026-10-15")] * 1.10  # 시가 +10% 갭 상승
        _run(db, "2026-10-15", inputs=inp)
        _run(db, "2026-10-16", inputs=inp)
        o = store.orders("shadow", db).iloc[0]
        assert 0 < o["filled_qty"] < o["qty"]
        assert "qty_reduced" in set(store.events(path=db)["kind"])
        assert store.sleeve_cash("shadow", db) >= 0  # 현금이 음수가 되지 않는다
        s = store.snapshots("shadow", path=db).iloc[-1]
        assert s["sleeve_value"] == pytest.approx(s["reference_value"], abs=1e-6)  # 백테스트도 같은 규칙

    def test_missing_open_is_stale(self, db):
        inp = _inputs(end="2026-10-16")
        inp.open = inp.open.drop(pd.Timestamp("2026-10-16"))
        out = _run(db, "2026-10-16", inputs=inp)
        assert out.status == "skipped_stale" and "시가 없음" in out.reason

    def test_shadow_matches_backtest_reference_exactly(self, db):
        """같은 포지션 정책·원장 계산·체결 시점(t 종가 결정 → t+1 시가 체결, 시가에 현금 부족이면 살 수 있는 만큼)이라
        섀도가 정상이면 매일 운용분 = 비교 기준이다. 단계가 0→1→0으로 바뀌고 가격·갭이 오르내리는 구간에서 본다."""
        inp = _inputs(end="2026-10-16")
        n = len(inp.close)
        inp.close[:] = np.round(10_000 * (1 + 0.04 * np.sin(np.arange(n) / 2)))
        inp.open = _opens(inp.close)
        days = ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15",
                "2026-10-16"]
        v = inp.values["KHJ_OVERHEAT"]
        v.loc[v["date"].isin(pd.DatetimeIndex(["2026-10-08", "2026-10-12", "2026-10-13"])), ["value", "is_on"]] = 1
        for d in days:
            assert _run(db, d, inputs=inp).status == "ok"
        s = store.snapshots("shadow", path=db)
        assert list(s["sleeve_value"]) == pytest.approx(list(s["reference_value"]), abs=1e-6)
        stages = store.shadow_runs(db)["stage"].tolist()
        assert stages == [0, 0, 1, 1, 1, 0, 0, 0]
        o = store.orders_shadow(db)
        assert {"2026-10-06", "2026-10-08", "2026-10-14"} <= set(o["decision_date"])  # 단계가 바뀐 날
        assert set(o.loc[~o["decision_date"].isin(["2026-10-06", "2026-10-08", "2026-10-14"]), "side"]) <= {"sell"}
        filled = store.orders("shadow", db)
        assert set(filled.loc[filled["decision_date"] < "2026-10-16", "status"]) <= {"filled", "partial"}


class TestSkips:
    @pytest.mark.parametrize("day,name", [("2026-10-09", "한글날"), ("2026-10-05", "개천절 대체공휴일"),
                                          ("2026-10-17", "토요일")])
    def test_holidays_including_substitute_are_skipped(self, db, day, name):
        out = _run(db, day, inputs=_inputs(end="2026-10-16"))
        assert out.status == "skipped_holiday" and name in out.reason
        assert store.orders_shadow(db).empty and store.orders(path=db).empty

    def test_outside_calendar_is_not_assumed_trading(self, db):
        out = _run(db, "2027-01-04", inputs=_inputs(end="2026-10-16"))
        assert out.status == "skipped_calendar" and "범위 밖" in out.reason

    def test_stale_close_is_skipped(self, db):
        out = _run(db, "2026-10-16", inputs=_inputs(end="2026-10-15"))
        assert out.status == "skipped_stale" and "종가 없음" in out.reason

    def test_stale_domestic_signal_is_skipped(self, db):
        out = _run(db, "2026-10-16", inputs=_inputs(end="2026-10-16", krx_signal_end="2026-10-15"))
        assert out.status == "skipped_stale" and "KHJ_OVERHEAT" in out.reason

    def test_stale_foreign_signal_is_skipped(self, db):
        out = _run(db, "2026-10-16", inputs=_inputs(end="2026-10-16", us_signal_end="2026-10-08"))
        assert out.status == "skipped_stale" and "YJH_US10Y" in out.reason

    def test_foreign_signal_from_previous_us_session_is_fresh(self, db):
        assert _run(db, "2026-10-16", inputs=_inputs(end="2026-10-16", us_signal_end="2026-10-15")).status == "ok"

    def test_not_today_and_before_close(self, db):
        d = pd.Timestamp("2026-10-15").date()
        out = shadow.run_shadow(d, datetime(2026, 10, 16, 16, 0, tzinfo=KST), ladder=LADDER, calendar=CAL, cfg=CFG,
                                inputs=_inputs(end="2026-10-16"), benchmark_close={T: None},
                                kill_switch_file=db.parent / "KS", path=db)
        assert out.status == "skipped_not_today"
        assert _run(db, "2026-10-16", hour=14).status == "skipped_before_close"
        assert store.orders_shadow(db).empty


class TestKillSwitch:
    def test_file_flag_blocks_and_is_recorded(self, db):
        f = db.parent / "KILL_SWITCH"
        f.write_text("")
        out = _run(db, "2026-10-16", ks_file=f)
        assert out.status == "blocked_kill_switch" and out.reason.startswith("킬 스위치로 차단됨")
        assert store.shadow_runs(db).iloc[-1]["reason"].startswith("킬 스위치로 차단됨")
        assert store.orders(path=db).empty

    def test_db_flag_blocks_and_off_resumes(self, db):
        f = db.parent / "KILL_SWITCH"
        assert ks_cli.main(["on", "--reason", "점검"], db_path=db, file_path=f) == 0
        assert _run(db, "2026-10-16").status == "blocked_kill_switch"
        ks_cli.main(["off", "--reason", "점검 끝"], db_path=db, file_path=f)
        assert _run(db, "2026-10-16").status == "ok"

    def test_kill_switch_engaged_mid_run_stops_remaining_orders(self, db, tmp_path):
        f = tmp_path / "KILL_SWITCH"

        class TouchingBroker(FakeBroker):
            def place_order(self, *a, **k):
                f.write_text("")  # 첫 주문 직후 누군가 킬 스위치를 켰다
                return super().place_order(*a, **k)

        b = TouchingBroker("shadow", cash=2_000_000, prices={"A": 1_000, "B": 1_000})
        r = engine.run_once(b, LADDER, "2026-10-16", {"A": 0.5, "B": 0.5}, {"A": 1_000, "B": 1_000},
                            path=db, kill_switch_file=f, fill="same_close")
        assert r.orders_sent == 1 and "kill_switch_stop" in set(store.events(path=db)["kind"])

    def test_kill_switch_cli_requires_reason(self, db):
        with pytest.raises(SystemExit):
            ks_cli.main(["on"], db_path=db, file_path=db.parent / "KS")


def test_concentration_cap_applies_to_shadow_target_and_reference_alike(db):
    """종목당 상한 30%: 단계 비중 100% → 30%, 섀도 운용분과 비교 기준이 여전히 정확히 같다."""
    cfg = {**CFG, "concentration": {"max_single_weight": 0.30, "exempt": ["069500"]}}
    inp = _inputs(end="2026-10-16")
    for day in ("2026-10-14", "2026-10-15", "2026-10-16"):
        d = pd.Timestamp(day).date()
        out = shadow.run_shadow(d, datetime(d.year, d.month, d.day, 16, 0, tzinfo=KST), ladder=LADDER, calendar=CAL,
                                cfg=cfg, inputs=inp, benchmark_close={T: None},
                                kill_switch_file=db.parent / "KILL_SWITCH", path=db)
        assert out.status == "ok" and out.target_weight == 0.30
    held = store.internal_positions("shadow", db)[T]
    assert held * float(inp.close["2026-10-15"]) <= 0.30 * 2_000_000  # 30% 안에서만 샀다
    snap = store.snapshots("shadow", path=db).iloc[-1]
    assert snap["sleeve_value"] == pytest.approx(snap["reference_value"], abs=1e-6)
    assert "concentration_cap" in set(store.events(path=db)["kind"])



def test_group_method_default_calibrates_on_kr_calendar_and_stays_equal_to_reference(db):
    """그룹 방식(기본): 기준점은 inp.calendar(2005~)에서 정하고, 섀도 운용분 = 비교 기준이 그대로 유지된다."""
    cal = pd.DatetimeIndex([d for d in pd.bdate_range("2004-06-01", "2026-10-16") if CAL.is_trading_day(d.date())
                            or d.year < 2026])
    inp = _inputs(end="2026-10-16")
    long_on = pd.Series((np.arange(len(cal)) % 9 == 0).astype(int), index=cal)
    inp.values["KHJ_OVERHEAT"] = _vals(cal, long_on.to_numpy())
    inp.calendar = cal
    cfg = {**CFG, "shadow": {**CFG["shadow"], "score_method": "group"}}
    for day in ("2026-10-15", "2026-10-16"):
        d = pd.Timestamp(day).date()
        out = shadow.run_shadow(d, datetime(d.year, d.month, d.day, 16, 0, tzinfo=KST), ladder=LADDER, calendar=CAL,
                                cfg=cfg, inputs=inp, benchmark_close={T: None},
                                kill_switch_file=db.parent / "KILL_SWITCH", path=db)
        assert out.status == "ok", out.reason
    snap = store.snapshots("shadow", path=db).iloc[-1]
    assert snap["sleeve_value"] == pytest.approx(snap["reference_value"], abs=1e-6)


def test_group_method_without_calibration_data_is_skipped_not_guessed(db):
    cfg = {**CFG, "shadow": {**CFG["shadow"], "score_method": "group"}}
    d = pd.Timestamp("2026-10-16").date()
    out = shadow.run_shadow(d, datetime(2026, 10, 16, 16, 0, tzinfo=KST), ladder=LADDER, calendar=CAL, cfg=cfg,
                            inputs=_inputs(end="2026-10-16"), benchmark_close={T: None},
                            kill_switch_file=db.parent / "KILL_SWITCH", path=db)
    assert out.status == "skipped_model" and "기준 기간" in out.reason
