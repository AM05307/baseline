"""
executor 전용 저장 계층(executor.db). baseline의 db/store.py와 별개 파일·별개 DB다.

append-only 테이블(tier_history·sleeve_ledger·exec_events·promotion_results·config_history·
contributions·savings_valuations)에는 INSERT만 한다. orders는 상태 갱신(UPDATE)이 있고,
snapshots는 (date, account)로 멱등 upsert한다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "executor.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"

ERROR_KINDS = ("order_error", "order_rejected")
MISMATCH_KINDS = ("reconcile_mismatch",)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def conn(path: Path | None = None):
    c = sqlite3.connect(path or DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db(path: Path | None = None) -> None:
    with conn(path) as c:
        c.executescript(SCHEMA.read_text())


def _df(sql: str, params: tuple = (), path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        return pd.read_sql_query(sql, c, params=params)


# ---------------------------------------------------------------- 단계 이력

def append_tier_event(effective_on: str, event: str, from_tier: str | None, to_tier: str,
                      operator: str, reason: str | None = None, path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute(
            "INSERT INTO tier_history (at, effective_on, event, from_tier, to_tier, operator, reason) "
            "VALUES (?,?,?,?,?,?,?)", (_now(), effective_on, event, from_tier, to_tier, operator, reason))


def tier_history(path: Path | None = None) -> pd.DataFrame:
    return _df("SELECT * FROM tier_history ORDER BY id", path=path)


def current_tier(path: Path | None = None) -> dict | None:
    """마지막 단계 기록 {to_tier, effective_on, event, ...}. 기록이 없으면 None."""
    with conn(path) as c:
        row = c.execute("SELECT * FROM tier_history ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 슬리브 원장

def append_ledger(date: str, account: str, kind: str, amount_krw: float, ticker: str | None = None,
                  qty: int = 0, price: float | None = None, order_key: str | None = None,
                  note: str | None = None, path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute(
            "INSERT INTO sleeve_ledger (at, date, account, kind, ticker, qty, price, amount_krw, order_key, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), date, account, kind, ticker, int(qty), price, float(amount_krw), order_key, note))


def sleeve_cash(account: str, path: Path | None = None) -> float:
    with conn(path) as c:
        v = c.execute("SELECT COALESCE(SUM(amount_krw), 0) FROM sleeve_ledger WHERE account=?", (account,)).fetchone()[0]
    return float(v)


def internal_positions(account: str, path: Path | None = None) -> dict[str, int]:
    """원장 기준 보유 수량(0주인 종목은 뺀다)."""
    with conn(path) as c:
        rows = c.execute(
            "SELECT ticker, SUM(qty) AS q FROM sleeve_ledger WHERE account=? AND ticker IS NOT NULL "
            "GROUP BY ticker", (account,)).fetchall()
    return {r["ticker"]: int(r["q"]) for r in rows if int(r["q"]) != 0}


# ---------------------------------------------------------------- 체결 완료 목표 비중

def append_targets(decision_date: str, account: str, targets: dict[str, float], path: Path | None = None) -> None:
    with conn(path) as c:
        c.executemany(
            "INSERT INTO target_history (recorded_at, decision_date, account, ticker, target_weight) VALUES (?, ?, ?, ?, ?)",
            [(_now(), decision_date, account, t, float(w)) for t, w in targets.items()])


def last_targets(account: str, path: Path | None = None) -> dict[str, float]:
    """종목별로 가장 최근에 기록된(체결까지 끝난) 목표 비중."""
    df = _df("SELECT ticker, target_weight FROM target_history WHERE account=? ORDER BY decision_date, id",
             (account,), path)
    return {} if df.empty else df.groupby("ticker")["target_weight"].last().to_dict()


# ---------------------------------------------------------------- 주문·사건

def get_order(order_key: str, path: Path | None = None) -> dict | None:
    with conn(path) as c:
        row = c.execute("SELECT * FROM orders WHERE order_key=?", (order_key,)).fetchone()
    return dict(row) if row else None


def insert_order(row: dict, path: Path | None = None) -> bool:
    """새 주문 기록. 같은 order_key가 이미 있으면 False(아무것도 안 바꿈) — 멱등 주문의 핵심."""
    with conn(path) as c:
        cur = c.execute(
            "INSERT INTO orders (order_key, created_at, decision_date, account, tier, ticker, side, qty, ref_price, status) "
            "VALUES (:order_key, :created_at, :decision_date, :account, :tier, :ticker, :side, :qty, :ref_price, 'planned') "
            "ON CONFLICT(order_key) DO NOTHING", {"created_at": _now(), **row})
        return cur.rowcount == 1


def update_order(order_key: str, path: Path | None = None, **fields) -> None:
    allowed = {"broker_order_id", "status", "filled_qty"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with conn(path) as c:
        c.execute(f"UPDATE orders SET {', '.join(f'{k}=?' for k in sets)} WHERE order_key=?",
                  (*sets.values(), order_key))


IN_FLIGHT_STATUSES = ("sending", "submitted")  # 보냈거나 보내는 중인데 결과가 확정되지 않은 주문


def in_flight_orders(account: str, path: Path | None = None) -> list[dict]:
    """재시작 복구 대상: 'sending'(전송 직전 기록 뒤 결과 모름)·'submitted'(브로커가 받았지만 결과 미확정)."""
    with conn(path) as c:
        rows = c.execute(f"SELECT * FROM orders WHERE account=? AND status IN ({','.join('?' * len(IN_FLIGHT_STATUSES))}) "
                         "ORDER BY created_at", (account, *IN_FLIGHT_STATUSES)).fetchall()
    return [dict(r) for r in rows]


def linked_broker_ids(account: str, path: Path | None = None) -> set[str]:
    """이미 우리 주문과 짝지어진 브로커 주문번호."""
    with conn(path) as c:
        rows = c.execute("SELECT broker_order_id FROM orders WHERE account=? AND broker_order_id IS NOT NULL",
                         (account,)).fetchall()
    return {r["broker_order_id"] for r in rows}


def ledger_qty_for_order(order_key: str, path: Path | None = None) -> int:
    """이 주문으로 원장에 이미 기록된 체결 수량(절댓값) — 복구 때 이중 기록을 막는다."""
    with conn(path) as c:
        v = c.execute("SELECT COALESCE(SUM(ABS(qty)), 0) FROM sleeve_ledger WHERE order_key=?", (order_key,)).fetchone()[0]
    return int(v)


def orders(account: str | None = None, path: Path | None = None) -> pd.DataFrame:
    if account:
        return _df("SELECT * FROM orders WHERE account=? ORDER BY created_at", (account,), path)
    return _df("SELECT * FROM orders ORDER BY created_at", path=path)


def append_event(date: str, account: str, kind: str, message: str = "", order_key: str | None = None,
                 path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute("INSERT INTO exec_events (at, date, account, kind, order_key, message) VALUES (?,?,?,?,?,?)",
                  (_now(), date, account, kind, order_key, message))


def events(account: str | None = None, since: str | None = None, path: Path | None = None) -> pd.DataFrame:
    sql, params = "SELECT * FROM exec_events WHERE 1=1", []
    if account:
        sql += " AND account=?"; params.append(account)
    if since:
        sql += " AND date>=?"; params.append(since)
    return _df(sql + " ORDER BY id", tuple(params), path)


FREEZE_KINDS = ("halt", "review_required")


def freeze_state(account: str, path: Path | None = None) -> str | None:
    """
    이 계좌의 정지 상태. 마지막 halt / review_required / resume 사건으로 정한다.
      - 'halt': 잔고 불일치 — 정리 주문을 포함한 모든 주문 정지
      - 'review_required': MDD·괴리·주문 오류로 강등 — 신규 주문 정지, 보유 유지, 수동 판단 필요
      - None: 정상(또는 resume됨)
    둘 다 사람이 executor.scripts.resume으로 해제해야 다시 주문한다.
    """
    with conn(path) as c:
        row = c.execute("SELECT kind FROM exec_events WHERE account=? AND kind IN ('halt','review_required','resume') "
                        "ORDER BY id DESC LIMIT 1", (account,)).fetchone()
    return row["kind"] if row and row["kind"] in FREEZE_KINDS else None


def is_halted(account: str, path: Path | None = None) -> bool:
    """정지(halt 또는 review_required) 상태면 True."""
    return freeze_state(account, path) is not None


# ---------------------------------------------------------------- 엔진 실행·킬 스위치·섀도

def append_engine_run(date: str, account: str, status: str, message: str = "", path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute("INSERT INTO engine_runs (at, date, account, status, message) VALUES (?,?,?,?,?)",
                  (_now(), date, account, status, message))


def trailing_status_streak(account: str, status: str, path: Path | None = None) -> int:
    """가장 최근 실행부터 거꾸로 세어 status가 연속으로 몇 번인지."""
    with conn(path) as c:
        rows = c.execute("SELECT status FROM engine_runs WHERE account=? ORDER BY id DESC", (account,)).fetchall()
    n = 0
    for r in rows:
        if r["status"] != status:
            break
        n += 1
    return n


def set_kill_switch(on: bool, reason: str, path: Path | None = None) -> None:
    if not reason:
        raise ValueError("킬 스위치 변경 사유를 적어야 합니다")
    with conn(path) as c:
        c.execute("INSERT INTO kill_switch_events (at, state, reason) VALUES (?,?,?)",
                  (_now(), "on" if on else "off", reason))


def kill_switch_db(path: Path | None = None) -> dict | None:
    """마지막 킬 스위치 기록이 'on'이면 그 행, 아니면 None."""
    with conn(path) as c:
        row = c.execute("SELECT * FROM kill_switch_events ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row and row["state"] == "on" else None


def append_shadow_run(row: dict, path: Path | None = None) -> None:
    full = {"reason": None, "risk_score": None, "stage": None, "target_weight": None, "capital": None, **row}
    with conn(path) as c:
        c.execute("INSERT INTO shadow_runs (at, decision_date, status, reason, risk_score, stage, target_weight, capital) "
                  "VALUES (:at, :decision_date, :status, :reason, :risk_score, :stage, :target_weight, :capital)",
                  {"at": _now(), **full})


def shadow_runs(path: Path | None = None) -> pd.DataFrame:
    return _df("SELECT * FROM shadow_runs ORDER BY id", path=path)


def insert_shadow_order(row: dict, path: Path | None = None) -> bool:
    with conn(path) as c:
        cur = c.execute(
            "INSERT INTO orders_shadow (decision_date, ticker, side, qty, ref_price, target_weight, risk_score, stage, recorded_at) "
            "VALUES (:decision_date, :ticker, :side, :qty, :ref_price, :target_weight, :risk_score, :stage, :recorded_at) "
            "ON CONFLICT(decision_date, ticker) DO NOTHING", {"recorded_at": _now(), **row})
        return cur.rowcount == 1


def orders_shadow(path: Path | None = None) -> pd.DataFrame:
    return _df("SELECT * FROM orders_shadow ORDER BY decision_date, ticker", path=path)


# ---------------------------------------------------------------- 스냅샷

def upsert_snapshot(row: dict, path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute("""
            INSERT INTO snapshots (date, account, tier, account_value, sleeve_value, operating_capital,
                                   outside_cash, reference_value, recorded_at)
            VALUES (:date, :account, :tier, :account_value, :sleeve_value, :operating_capital,
                    :outside_cash, :reference_value, :recorded_at)
            ON CONFLICT(date, account) DO UPDATE SET tier=excluded.tier, account_value=excluded.account_value,
                sleeve_value=excluded.sleeve_value, operating_capital=excluded.operating_capital,
                outside_cash=excluded.outside_cash, reference_value=excluded.reference_value,
                recorded_at=excluded.recorded_at
        """, {"reference_value": None, "account_value": None, "outside_cash": None, "recorded_at": _now(), **row})


def snapshots(account: str, since: str | None = None, path: Path | None = None) -> pd.DataFrame:
    sql, params = "SELECT * FROM snapshots WHERE account=?", [account]
    if since:
        sql += " AND date>=?"; params.append(since)
    return _df(sql + " ORDER BY date", tuple(params), path)


# ---------------------------------------------------------------- 승격 결과·설정 이력

def append_promotion_result(tier: str, next_tier: str | None, eligible: bool, details: dict,
                            path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute("INSERT INTO promotion_results (at, tier, next_tier, eligible, details) VALUES (?,?,?,?,?)",
                  (_now(), tier, next_tier, int(eligible), json.dumps(details, ensure_ascii=False, default=str)))


def promotion_results(path: Path | None = None) -> pd.DataFrame:
    return _df("SELECT * FROM promotion_results ORDER BY id", path=path)


def record_config_version(file_path: str, content: str, path: Path | None = None) -> bool:
    """내용 해시가 그 파일의 직전 기록과 다르면 한 행 추가하고 True. 같으면 아무것도 안 함."""
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with conn(path) as c:
        last = c.execute("SELECT sha256 FROM config_history WHERE path=? ORDER BY id DESC LIMIT 1",
                         (file_path,)).fetchone()
        if last and last["sha256"] == sha:
            return False
        c.execute("INSERT INTO config_history (at, path, sha256, content) VALUES (?,?,?,?)",
                  (_now(), file_path, sha, content))
    return True


def config_history(path: Path | None = None) -> pd.DataFrame:
    return _df("SELECT id, at, path, sha256 FROM config_history ORDER BY id", path=path)


# ---------------------------------------------------------------- 적립 기록(수동)

def add_contribution(date: str, amount_krw: float, destination: str, note: str | None = None,
                     path: Path | None = None) -> None:
    if destination not in ("savings", "trading"):
        raise ValueError("destination은 'savings'(적립 계좌) 또는 'trading'(자동매매 계좌)")
    if amount_krw <= 0:
        raise ValueError("납입액은 양수여야 합니다")
    with conn(path) as c:
        c.execute("INSERT INTO contributions (at, date, amount_krw, destination, note) VALUES (?,?,?,?,?)",
                  (_now(), date, float(amount_krw), destination, note))


def contributions(path: Path | None = None) -> pd.DataFrame:
    return _df("SELECT * FROM contributions ORDER BY date, id", path=path)


def add_savings_valuation(as_of: str, value_krw: float, note: str | None = None, path: Path | None = None) -> None:
    if value_krw < 0:
        raise ValueError("평가금액은 0 이상이어야 합니다")
    with conn(path) as c:
        c.execute("INSERT INTO savings_valuations (at, as_of, value_krw, note) VALUES (?,?,?,?)",
                  (_now(), as_of, float(value_krw), note))


def latest_savings_valuation(path: Path | None = None) -> dict | None:
    with conn(path) as c:
        row = c.execute("SELECT * FROM savings_valuations ORDER BY as_of DESC, id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 다음 날 시가 체결 대기(fill=next_open)

def add_pending_decision(decision_date: str, account: str, targets: dict[str, float], path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute("INSERT INTO pending_decisions (decision_date, account, targets, status, recorded_at, updated_at) "
                  "VALUES (?,?,?,'pending',?,?) ON CONFLICT(decision_date, account) DO NOTHING",
                  (decision_date, account, json.dumps(targets), _now(), _now()))


def pending_decisions(account: str, before: str, path: Path | None = None) -> list[dict]:
    """before(체결일)보다 앞선 결정 중 아직 체결 대기인 것, 오래된 순."""
    with conn(path) as c:
        rows = c.execute("SELECT * FROM pending_decisions WHERE account=? AND status='pending' AND decision_date<? "
                         "ORDER BY decision_date", (account, before)).fetchall()
    return [{**dict(r), "targets": json.loads(r["targets"])} for r in rows]


def set_decision_status(decision_date: str, account: str, status: str, path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute("UPDATE pending_decisions SET status=?, updated_at=? WHERE decision_date=? AND account=?",
                  (status, _now(), decision_date, account))


def planned_orders(decision_date: str, account: str, path: Path | None = None) -> list[dict]:
    with conn(path) as c:
        rows = c.execute("SELECT * FROM orders WHERE decision_date=? AND account=? AND status='planned' "
                         "ORDER BY CASE side WHEN 'sell' THEN 0 ELSE 1 END, ticker", (decision_date, account)).fetchall()
    return [dict(r) for r in rows]


def record_gap(decision_date: str, ticker: str, decision_close: float, fill_date: str, fill_open: float,
               path: Path | None = None) -> bool:
    with conn(path) as c:
        cur = c.execute(
            "INSERT INTO shadow_gaps (decision_date, ticker, decision_close, fill_date, fill_open, gap_pct, recorded_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(decision_date, ticker) DO NOTHING",
            (decision_date, ticker, float(decision_close), fill_date, float(fill_open),
             (float(fill_open) / float(decision_close) - 1) * 100, _now()))
        return cur.rowcount == 1


def shadow_gaps(path: Path | None = None) -> pd.DataFrame:
    return _df("SELECT * FROM shadow_gaps ORDER BY decision_date", path=path)
