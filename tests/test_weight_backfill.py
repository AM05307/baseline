"""
analysis/weight_backfill.py · scripts/backfill_weights.py · weight_history.source 마이그레이션 테스트.

핵심: (1) t 시점 가중치가 t 이후 데이터를 바꿔도 변하지 않는다(미래참조 방지),
(2) 재실행해도 행이 늘지 않고 기존 행이 안 바뀐다(append-only·멱등),
(3) 실시간 경로(recompute_weights.py)와 같은 함수·같은 결과.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import weight_backfill as wb  # noqa: E402
from db import store  # noqa: E402
from scripts import backfill_weights, recompute_weights  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _world(n: int = 3000, seed: int = 0, event_every: int = 25, start: str = "2010-01-04"):
    """
    close: 무작위 보행. GOOD 신호는 on인 날 뒤 20거래일에 하락 드리프트를 심어서 risk 방향 적중률이
    기준률(≈50%)을 확실히 넘는다. NOISE 신호는 무작위라 유의하지 않다.
    반환: (close, signals, is_on_by_signal)
    """
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range(start, periods=n)
    rets = rs.normal(0.0003, 0.01, n)
    good_on = np.zeros(n, dtype=int)
    for d in range(60, n - 25, event_every):
        good_on[d] = 1
        rets[d + 1:d + 21] -= 0.006  # 20일간 -0.6%/일 → 확실한 하락
    close = pd.Series(100 * np.cumprod(1 + rets), index=idx)
    noise_on = (rs.rand(n) < 0.03).astype(int)
    signals = [
        {"signal_id": "GOOD", "direction": "risk", "expert_id": "E1", "weight_cap_pct": None},
        {"signal_id": "NOISE", "direction": "risk", "expert_id": "E2", "weight_cap_pct": float("nan")},
        {"signal_id": "EVENT_ONLY", "direction": None, "expert_id": "E2", "weight_cap_pct": None},
    ]
    return close, signals, {"GOOD": pd.Series(good_on, index=idx), "NOISE": pd.Series(noise_on, index=idx)}


class TestComputeWeightsAsOf:
    def test_good_signal_gets_weight_and_event_only_row_is_skipped(self):
        close, signals, on = _world()
        snap = wb.compute_weights_as_of(close, signals, on)
        assert set(snap.signal_ids) == {"GOOD", "NOISE"}  # direction이 None인 행은 가중치 대상 아님
        assert snap.evidence_based and snap.evidence_signals >= 1
        assert snap.weights["GOOD"] > snap.weights["NOISE"]
        # 신호당 상한(기본 30%)이 걸리면 남는 몫을 받을 신호가 없어 합이 100 미만일 수 있다(기존 알고리즘 동작).
        assert snap.weights["GOOD"] == pytest.approx(wb.wt.DEFAULT_SIGNAL_CAP_PCT)
        assert sum(snap.weights.values()) <= 100.0 + 1e-9

    def test_no_future_leak_changing_data_after_t_does_not_change_weights(self):
        close, signals, on = _world()
        t = close.index[2000]
        base = wb.compute_weights_as_of(close, signals, on, as_of=t)

        tampered_close = close.copy()
        tampered_close.loc[tampered_close.index > t] *= np.linspace(1, 9, (close.index > t).sum())
        tampered_on = {k: v.copy() for k, v in on.items()}
        for v in tampered_on.values():
            v.loc[v.index > t] = 1 - v.loc[v.index > t]
        again = wb.compute_weights_as_of(tampered_close, signals, tampered_on, as_of=t)

        assert again.weights == base.weights
        assert again.as_of == base.as_of == t
        assert again.n_close == base.n_close
        for sid in base.in_sample:
            a, b = base.in_sample[sid], again.in_sample[sid]
            assert (a.n_events, a.hit_rate_pct_events, a.ci_lower_pct, a.base_rate_pct) == \
                   (b.n_events, b.hit_rate_pct_events, b.ci_lower_pct, b.base_rate_pct)

    def test_control_changing_data_before_t_does_change_weights(self):
        # 위 테스트가 "아무것도 안 바뀌는 둔한 함수"라서 통과하는 게 아님을 보이는 대조군.
        close, signals, on = _world()
        t = close.index[2000]
        base = wb.compute_weights_as_of(close, signals, on, as_of=t)
        flipped = {k: v.copy() for k, v in on.items()}
        flipped["GOOD"] = pd.Series(np.random.RandomState(5).randint(0, 2, len(close)), index=close.index)
        changed = wb.compute_weights_as_of(close, signals, flipped, as_of=t)
        assert changed.weights != base.weights

    def test_as_of_none_uses_everything_and_differs_from_earlier_t(self):
        close, signals, on = _world()
        full = wb.compute_weights_as_of(close, signals, on)
        early = wb.compute_weights_as_of(close, signals, on, as_of=close.index[800])
        assert full.n_close == len(close)
        assert early.n_close == 801
        assert full.as_of == close.index[-1]

    def test_short_history_falls_back_to_equal_weights_and_is_flagged(self):
        close, signals, on = _world()
        snap = wb.compute_weights_as_of(close, signals, on, as_of=close.index[100])
        assert not snap.evidence_based
        assert snap.weights["GOOD"] == pytest.approx(snap.weights["NOISE"])

    def test_empty_close_does_not_crash(self):
        _, signals, on = _world()
        snap = wb.compute_weights_as_of(pd.Series(dtype=float), signals, on)
        assert snap.as_of is None and snap.n_close == 0
        assert not snap.evidence_based

    def test_signal_without_values_is_candidate_with_zero_evidence(self):
        close, signals, on = _world()
        snap = wb.compute_weights_as_of(close, signals, {"GOOD": on["GOOD"]})  # NOISE 값 없음
        assert "NOISE" in snap.weights


class TestMonthEndGrid:
    def test_last_trading_day_of_each_month(self):
        days = pd.bdate_range("2024-01-01", "2024-06-30")
        grid = wb.month_end_grid(days, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-06-30"))
        assert [d.strftime("%Y-%m-%d") for d in grid] == [
            "2024-01-31", "2024-02-29", "2024-03-29", "2024-04-30", "2024-05-31", "2024-06-28",
        ]
        assert all(d in days for d in grid)  # 달력 월말이 아니라 실제 거래일 (3/31, 6/30은 주말)

    def test_every_three_months_and_bounds(self):
        days = pd.bdate_range("2024-01-01", "2024-12-31")
        grid = wb.month_end_grid(days, pd.Timestamp("2024-02-01"), pd.Timestamp("2024-10-31"), every_months=3)
        assert [d.month for d in grid] == [2, 5, 8]
        assert wb.month_end_grid(days, pd.Timestamp("2030-01-01"), pd.Timestamp("2030-12-31")) == []
        with pytest.raises(ValueError):
            wb.month_end_grid(days, days[0], days[-1], every_months=0)


class TestStoreMigrationAndIdempotency:
    def test_legacy_table_without_source_is_migrated_with_live_default(self, tmp_path):
        path = tmp_path / "legacy.db"
        c = sqlite3.connect(path)
        c.execute("""CREATE TABLE weight_history (id INTEGER PRIMARY KEY AUTOINCREMENT, recomputed_on TEXT NOT NULL,
                     signal_id TEXT NOT NULL, old_weight_pct REAL, new_weight_pct REAL NOT NULL, reason TEXT)""")
        c.execute("INSERT INTO weight_history (recomputed_on, signal_id, new_weight_pct) VALUES ('2026-10-06','A',7.0)")
        c.commit(); c.close()

        store.init_db(path)
        store.init_db(path)  # 두 번 돌려도 안전
        wh = store.get_weight_history(path=path)
        assert list(wh["source"]) == ["live"]
        assert store.get_current_weights(path=path) == {"A": 7.0}

    def test_backfill_rows_idempotent_live_rows_unchanged_semantics(self, tmp_db):
        bf = {"recomputed_on": "2020-01-31", "signal_id": "A", "old_weight_pct": None,
              "new_weight_pct": 5.0, "reason": "r", "source": "backfill"}
        assert store.insert_weight_history([bf], path=tmp_db) == 1
        assert store.insert_weight_history([{**bf, "new_weight_pct": 99.0}], path=tmp_db) == 0  # 덮어쓰지 않음
        wh = store.get_weight_history(path=tmp_db)
        assert len(wh) == 1 and wh.iloc[0]["new_weight_pct"] == 5.0

        live = {**bf, "source": "live"}
        store.insert_weight_history([live], path=tmp_db)
        store.insert_weight_history([live], path=tmp_db)  # live는 기존 동작 그대로 append
        assert (store.get_weight_history(path=tmp_db)["source"] == "live").sum() == 2

    def test_not_null_violation_still_raises(self, tmp_db):
        bad = {"recomputed_on": "2020-01-31", "signal_id": "A", "old_weight_pct": None,
               "new_weight_pct": None, "reason": "r", "source": "backfill"}
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_weight_history([bad], path=tmp_db)


def _seed(tmp_db, live_row_date: str | None = None):
    close, signals, on = _world(n=1500, start="2015-01-05")
    store.upsert_prices(
        [{"ticker": "000660", "date": d.strftime("%Y-%m-%d"), "open": float(c), "high": float(c),
          "low": float(c), "close": float(c), "volume": 1, "source": "test"} for d, c in close.items()],
        path=tmp_db,
    )
    store.upsert_expert({"expert_id": "E1", "name": "e1"}, path=tmp_db)
    store.upsert_expert({"expert_id": "E2", "name": "e2"}, path=tmp_db)
    for s in signals[:2]:
        store.upsert_signal({
            "signal_id": s["signal_id"], "name": s["signal_id"], "expert_id": s["expert_id"],
            "kind": "trade_rule", "direction": s["direction"], "description": "d",
        }, path=tmp_db)
        store.upsert_signal_values(
            [{"date": d.strftime("%Y-%m-%d"), "signal_id": s["signal_id"], "value": None, "is_on": int(v)}
             for d, v in on[s["signal_id"]].items()], path=tmp_db)
    if live_row_date:
        store.insert_weight_history([{
            "recomputed_on": live_row_date, "signal_id": sid, "old_weight_pct": None,
            "new_weight_pct": 50.0, "reason": "live",
        } for sid in ("GOOD", "NOISE")], path=tmp_db)
    return close


def _raw_rows(db_path, source: str) -> list[tuple]:
    """pandas를 거치지 않고 SQL로 직접 읽은 행(id 포함) — NULL/NaN 표현 차이에 안 흔들린다."""
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT * FROM weight_history WHERE source=? ORDER BY id", (source,)).fetchall()
    finally:
        c.close()


def _run(monkeypatch, tmp_db, *argv):
    monkeypatch.setattr(store, "DB_PATH", tmp_db)
    monkeypatch.setattr(sys, "argv", ["backfill_weights.py", "--every-months", "12", "--no-report", *argv])
    backfill_weights.main()


class TestBackfillScript:
    def test_rerun_is_idempotent_and_never_modifies_existing_rows(self, tmp_db, monkeypatch, capsys):
        _seed(tmp_db, live_row_date="2021-06-01")
        live_before = _raw_rows(tmp_db, "live")
        n_live = len(live_before)

        _run(monkeypatch, tmp_db)
        after1 = store.get_weight_history(path=tmp_db)
        assert len(after1) > n_live
        assert set(after1[after1["source"] == "backfill"]["signal_id"]) == {"GOOD", "NOISE"}

        backfill_rows_1 = _raw_rows(tmp_db, "backfill")
        _run(monkeypatch, tmp_db)  # 두 번째 실행
        after2 = store.get_weight_history(path=tmp_db)
        assert len(after2) == len(after1)  # 행이 늘지 않음
        assert _raw_rows(tmp_db, "backfill") == backfill_rows_1  # 백필 행은 값까지 그대로
        assert _raw_rows(tmp_db, "live") == live_before  # 기존 live 행은 한 칸도 안 바뀜

    def test_backfill_stays_strictly_before_first_live_row(self, tmp_db, monkeypatch):
        _seed(tmp_db, live_row_date="2019-03-01")
        _run(monkeypatch, tmp_db)
        wh = store.get_weight_history(path=tmp_db)
        bf = wh[wh["source"] == "backfill"]
        assert not bf.empty
        assert (pd.to_datetime(bf["recomputed_on"]) < pd.Timestamp("2019-03-01")).all()
        # '현재 가중치'는 여전히 live 행의 값이다
        assert store.get_current_weights(path=tmp_db) == {"GOOD": 50.0, "NOISE": 50.0}

    def test_old_weight_chain_and_reason_fields(self, tmp_db, monkeypatch):
        _seed(tmp_db)
        _run(monkeypatch, tmp_db)
        bf = store.get_weight_history("GOOD", path=tmp_db).sort_values("recomputed_on").reset_index(drop=True)
        assert len(bf) >= 3
        assert pd.isna(bf.loc[0, "old_weight_pct"])
        for i in range(1, len(bf)):
            assert bf.loc[i, "old_weight_pct"] == pytest.approx(bf.loc[i - 1, "new_weight_pct"])
        assert (bf["source"] == "backfill").all()
        assert bf["reason"].str.contains("evidence_signals=").all()
        assert backfill_weights.evidence_flag(bf.loc[0, "reason"]) in (True, False)

    def test_backfill_rows_equal_pure_function_at_that_date(self, tmp_db, monkeypatch):
        close = _seed(tmp_db)
        _, signals, on = _world(n=1500, start="2015-01-05")
        _run(monkeypatch, tmp_db)
        bf = store.get_weight_history(path=tmp_db)
        for d, grp in bf.groupby("recomputed_on"):
            snap = wb.compute_weights_as_of(close, signals, on, as_of=pd.Timestamp(d))
            for _, r in grp.iterrows():
                assert r["new_weight_pct"] == pytest.approx(snap.weights[r["signal_id"]])

    def test_cold_start_shrinks_and_dry_run_writes_nothing(self, tmp_db, monkeypatch, capsys):
        close = _seed(tmp_db)
        _run(monkeypatch, tmp_db, "--dry-run")
        assert store.get_weight_history(path=tmp_db).empty
        assert "--dry-run" in capsys.readouterr().out

        sids = ["GOOD", "NOISE"]
        before = backfill_weights.cold_start_days(store.get_weight_history(path=tmp_db), close.index, sids)
        _run(monkeypatch, tmp_db)
        after = backfill_weights.cold_start_days(store.get_weight_history(path=tmp_db), close.index, sids)
        assert before == len(close)
        assert 0 < after < before
        assert "냉시작" in capsys.readouterr().out

    def test_recompute_weights_live_path_matches_shared_function(self, tmp_db, monkeypatch):
        close = _seed(tmp_db)
        _, signals, on = _world(n=1500, start="2015-01-05")
        monkeypatch.setattr(sys, "argv", ["recompute_weights.py"])
        recompute_weights.main()
        wh = store.get_weight_history(path=tmp_db)
        assert (wh["source"] == "live").all()
        snap = wb.compute_weights_as_of(close, signals, on)
        for _, r in wh.iterrows():
            assert r["new_weight_pct"] == pytest.approx(snap.weights[r["signal_id"]])


class TestHelpers:
    def test_evidence_flag_parsing(self):
        assert backfill_weights.evidence_flag("backfill as_of=2020-01-31 evidence_signals=2/11 min_events=30") is True
        assert backfill_weights.evidence_flag("backfill evidence_signals=0/11") is False
        assert backfill_weights.evidence_flag("in_sample n_events=3 ...") is None
        assert backfill_weights.evidence_flag(None) is None

    def test_days_under_snapshots_partitions_all_days(self):
        days = pd.bdate_range("2024-01-01", periods=100)
        snaps = [
            {"date": days[20].date().isoformat(), "evidence_based": False, "computed": True},
            {"date": days[60].date().isoformat(), "evidence_based": True, "computed": True},
        ]
        out = backfill_weights.days_under_snapshots(snaps, days)
        assert out == {"evidence": 40, "fallback": 40, "unknown": 0, "none": 20}
        assert sum(out.values()) == 100
        assert backfill_weights.days_under_snapshots([], days)["none"] == 100
