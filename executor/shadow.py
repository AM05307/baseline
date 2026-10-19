"""
섀도 실행기 — 매 거래일 장 마감 후, baseline 위험 점수 모델이 정한 목표 비중으로 "보냈을 주문"을 계산해
orders_shadow에 기록한다. 실제 브로커로는 아무것도 보내지 않는다: 주문은 executor 원장 안에서만 그날 종가에
체결된 것으로 처리한다(ShadowBroker, FakeBroker 계열). 그래서 섀도 계좌의 운용분·스냅샷이 쌓이고, 이것이
모의투자·실제 계좌의 비교 기준이 된다.

실행하지 않는 경우(사유만 shadow_runs에 남김):
  - 킬 스위치(파일 또는 DB)                         → blocked_kill_switch "킬 스위치로 차단됨: …"
  - 캘린더 범위 밖                                   → skipped_calendar
  - 주말·휴장일(대체공휴일 포함)                     → skipped_holiday
  - 오늘이 아닌 날짜, 장 마감 전                     → skipped_not_today / skipped_before_close
  - 데이터 신선도(결정일 종가 없음, 신호값 오래됨,
    가중치 기록 없음)                                → skipped_stale
  - 섀도 계좌 정지 상태                               → blocked_frozen

**앞으로만 돈다.** 과거 날짜로 돌리는 기능은 없다 — 지난 날짜의 성과는 analysis/what_if(백테스트)가 측정하고,
섀도는 "그날 실제로 알 수 있었던 데이터로 그날 계산했다"는 기록이어야 의미가 있다.

목표 비중 계산은 scripts/what_if_recent.py와 같은 경로다: 결정일 정렬(analysis/signal_alignment) + 그 날짜
이전 weight_history + 분리 방식 위험 점수(analysis/what_if.score_and_stage) → 단계 → 단계별 비중.
baseline의 analysis·db는 읽기만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from analysis import signal_alignment as sa
from analysis.position_policy import PositionPolicy
from analysis import what_if as wi
from executor import config_files, engine, kill_switch, metrics, store
from executor.broker.fake import FakeBroker
from executor.planner import DailyLimits
from executor.krx_calendar import KrxCalendar, next_year_warning
from executor.tiers import Ladder

KST = ZoneInfo("Asia/Seoul")
MARKET_CLOSE_KST = time(15, 40)  # 정규장 15:30 마감 + 종가 확정 여유
SHADOW_ACCOUNT = "shadow"
RECENT_SIGNAL_DAYS = 30  # 이 기간 안에 값이 하나라도 있는 신호만 신선도 검사 대상(수동 입력이 아예 없는 신호는 제외)


class ShadowBroker(FakeBroker):
    """섀도 원장 상태로 만든 가상 브로커 — 주문은 받은 가격(종가 또는 시가)으로 즉시 전량 체결된 것으로 친다.
    실행마다 원장에서 새로 만들므로 브로커 상태 = 원장이다(재시작 복구에서 ledger_is_broker로 쓴다)."""

    ledger_is_broker = True

    def __init__(self, ticker: str, price: float, path: Path | None = None):
        super().__init__(account_kind=SHADOW_ACCOUNT, cash=store.sleeve_cash(SHADOW_ACCOUNT, path),
                         prices={ticker: price}, positions=store.internal_positions(SHADOW_ACCOUNT, path),
                         commission_rate=engine.DEFAULT_COMMISSION, tax_rate=engine.DEFAULT_TAX)


@dataclass
class BaselineInputs:
    close: pd.Series
    signal_ids: list[str]
    directions: dict[str, str]
    values: dict[str, pd.DataFrame]
    weight_history: pd.DataFrame
    open: pd.Series | None = None  # 시가 — fill=next_open(기본)에 필요
    calendar: pd.DatetimeIndex | None = None  # 그룹 방식 기준점을 정할 한국 거래일(KOSPI 날짜). 없으면 close 날짜


def load_baseline_inputs(ticker: str) -> BaselineInputs:
    """baseline.db를 읽기만 한다."""
    from db import store as baseline_store

    sig = baseline_store.get_signals(enabled_only=True)
    sig = sig[sig["direction"].isin(["risk", "opportunity"])]
    ids = list(sig["signal_id"])
    ohlc = baseline_store.get_ohlc(ticker)
    opens = ohlc["open"].sort_index() if not ohlc.empty else None  # get_ohlc는 date 인덱스
    return BaselineInputs(
        close=baseline_store.get_prices(ticker).sort_index(), signal_ids=ids, open=opens,
        directions=dict(zip(sig["signal_id"], sig["direction"])),
        values={s: baseline_store.get_signal_values(s) for s in ids},
        weight_history=baseline_store.get_weight_history(),
        calendar=baseline_store.get_prices("KOSPI").index,
    )


def freshness_problems(inp: BaselineInputs, t: pd.Timestamp, max_foreign_age_days: int,
                       need_open: bool = False, method: str = wi.DEFAULT_SCORE_METHOD) -> list[str]:
    """비어 있으면 신선. 결정일 종가·신호값·가중치(그리고 next_open이면 오늘 시가)가 결정일 기준으로 최신인지 본다."""
    problems = []
    if need_open and (inp.open is None or t not in inp.open.index or pd.isna(inp.open.get(t))):
        problems.append(f"{t.date()} 시가 없음 — 전날 결정 주문을 시가로 체결할 수 없음")
    if inp.close.empty or t not in inp.close.index:
        last = inp.close.index.max().date() if not inp.close.empty else "없음"
        problems.append(f"결정일 {t.date()} 종가 없음(마지막 시세일 {last}) — collectors/collect_daily.py 먼저")
    for sid in inp.signal_ids:
        v = inp.values.get(sid)
        if v is None or v.empty or pd.to_datetime(v["date"]).max() < t - pd.Timedelta(days=RECENT_SIGNAL_DAYS):
            continue  # 원래 값이 없는 신호(수동 입력 미입력 등)는 신선도 대상 아님 — 점수 상한 경고가 따로 다룬다
        prior = sid in sa.PRIOR_SESSION_SIGNALS
        src = sa.align_values(v, pd.DatetimeIndex([t]), prior)["src_date"].iloc[0]
        if pd.isna(src):
            problems.append(f"{sid}: 결정일 기준 값 없음 — scripts/compute_signals.py 먼저")
        elif not prior and pd.Timestamp(src) != t:
            problems.append(f"{sid}: 마지막 값 {pd.Timestamp(src).date()} (결정일 값 필요)")
        elif prior and (t - pd.Timestamp(src)).days > max_foreign_age_days:
            problems.append(f"{sid}: 해외 세션 마지막 값 {pd.Timestamp(src).date()} ({max_foreign_age_days}일 초과)")
    if method == "weighted":  # 그룹 방식은 가중치 기록을 쓰지 않는다
        w = wi.weights_for_dates(inp.weight_history, pd.DatetimeIndex([t]), inp.signal_ids)
        if w.notna().sum(axis=1).iloc[0] == 0:
            problems.append("결정일 이전 weight_history 기록 없음(냉시작)")
    return problems


def model_states(inp: BaselineInputs, dates: pd.DatetimeIndex, combo_stage: int,
                 method: str = wi.DEFAULT_SCORE_METHOD) -> pd.DataFrame:
    """
    method="group"(기본): 신호 그룹 균등 가중치 + 기준 기간(2005~2018) 백분위 단계 — 기준점은 inp.calendar(한국 거래일)
    전체에서 정한다(대상 069500 시세가 2007년부터라도 같은 기준 기간). "weighted": 예전 적중률 가중치.
    """
    if method == "group":
        grid = pd.DatetimeIndex(dates)
        if inp.calendar is not None and len(inp.calendar):
            grid = grid.union(pd.DatetimeIndex(inp.calendar))
        is_on_all, _ = sa.align_signals(inp.values, grid)
        return wi.group_states(is_on_all, dates)
    if method != "weighted":
        raise ValueError(f"score_method는 {wi.SCORE_METHODS} 중 하나: {method}")
    is_on, value = sa.align_signals(inp.values, dates)
    weights = wi.weights_for_dates(inp.weight_history, dates, inp.signal_ids)
    combo = value["KHJ_COMBO"] if "KHJ_COMBO" in value.columns else None
    return wi.score_and_stage(is_on, weights, inp.directions, combo, combo_stage=combo_stage)


def shadow_policy() -> PositionPolicy:
    """섀도 엔진과 비교 기준 백테스트가 같이 쓰는 포지션 정책(목표 변경 시에만, 정수 주식, 매수 수수료 반영)."""
    return PositionPolicy(buy_cost_rate=engine.DEFAULT_COMMISSION)


def backtest_reference(inp: BaselineInputs, start: pd.Timestamp, t: pd.Timestamp, stage_weights: list[float],
                       combo_stage: int, capital: float, fill: str = engine.DEFAULT_FILL,
                       method: str = wi.DEFAULT_SCORE_METHOD) -> float:
    """
    섀도 시작일(start)부터 같은 규칙을 정수 주식 백테스트(analysis/what_if.simulate_shares)로 돌린 t일 자산 — 괴리
    비교 기준. 엔진과 같은 포지션 정책·같은 원장 계산·같은 체결 시점(기본: t일 종가 결정 → t+1일 시가 체결, 시가에
    현금이 모자라면 살 수 있는 만큼)이라, 섀도가 정상이면 수량 경로와 자산이 정확히 같다. 남는 괴리는 실행 쪽
    (부분 체결·거부·지연·정지·만료)에서만 생긴다.
    """
    dates = inp.close.index[(inp.close.index >= start) & (inp.close.index <= t)]
    if len(dates) == 0:
        return float(capital)
    states = model_states(inp, dates, combo_stage, method)
    target = states["stage"].map(dict(enumerate(stage_weights)))
    sim = wi.simulate_shares(inp.close[dates], target, capital, shadow_policy(), engine.DEFAULT_COMMISSION,
                             lambda d: engine.DEFAULT_TAX, fill=fill,
                             open_=inp.open[dates] if inp.open is not None else None)
    return float(sim["equity"].iloc[-1])


@dataclass
class ShadowOutcome:
    status: str
    reason: str = ""
    risk_score: float | None = None
    stage: int | None = None
    target_weight: float | None = None
    orders: list = field(default_factory=list)
    engine_result: engine.RunResult | None = None
    fill_result: engine.RunResult | None = None  # next_open: 오늘 시가에 체결한 전날 결정
    gap_pct: float | None = None  # 직전 거래일 종가 대비 오늘 시가(%)
    warnings: list = field(default_factory=list)  # 실행을 막지 않는 알림(예: 다음 해 휴장일 목록 없음)


def _record(t: date, out: ShadowOutcome, capital: float | None, path) -> ShadowOutcome:
    store.append_shadow_run({"decision_date": t.isoformat(), "status": out.status, "reason": out.reason,
                             "risk_score": out.risk_score, "stage": out.stage, "target_weight": out.target_weight,
                             "capital": capital}, path=path)
    return out


def run_shadow(decision_date: date | None = None, now: datetime | None = None, *, ladder: Ladder | None = None,
               calendar: KrxCalendar | None = None, cfg: dict | None = None, inputs: BaselineInputs | None = None,
               benchmark_close=None, kill_switch_file: Path | None = None, path: Path | None = None) -> ShadowOutcome:
    store.init_db(path)
    now = now or datetime.now(KST)
    calendar = calendar or config_files.load_calendar(db_path=path)
    out = _run_shadow(decision_date, now, ladder=ladder, calendar=calendar, cfg=cfg, inputs=inputs,
                      benchmark_close=benchmark_close, kill_switch_file=kill_switch_file, path=path)
    warn = next_year_warning(calendar, now.astimezone(KST).date())
    if warn:  # 실행 결과와 무관하게 매번 남긴다(로그 = exec_events, CLI가 출력)
        out.warnings.append(warn)
        store.append_event((decision_date or now.date()).isoformat(), SHADOW_ACCOUNT, "calendar_warning", warn, path=path)
    return out


def _run_shadow(decision_date: date | None, now: datetime, *, ladder: Ladder | None, calendar: KrxCalendar,
                cfg: dict | None, inputs: BaselineInputs | None, benchmark_close, kill_switch_file: Path | None,
                path: Path | None) -> ShadowOutcome:
    today = now.date()
    t = decision_date or today
    cfg = cfg or config_files.load_executor_config(db_path=path)
    ladder = ladder or config_files.load_ladder(db_path=path)
    ks_file = kill_switch_file if kill_switch_file is not None else config_files.resolve(cfg["kill_switch_file"])

    ks = kill_switch.engaged(ks_file, path)
    if ks:
        return _record(t, ShadowOutcome("blocked_kill_switch", f"킬 스위치로 차단됨: {ks}"), None, path)
    trading = calendar.is_trading_day(t)
    if trading is None:
        return _record(t, ShadowOutcome("skipped_calendar",
                                        f"휴장일 캘린더 범위 밖({calendar.covered_from}~{calendar.covered_to}) — "
                                        "config/krx_holidays.yaml에 해당 연도 추가 필요"), None, path)
    if trading is False:
        return _record(t, ShadowOutcome("skipped_holiday", f"휴장일: {calendar.holiday_name(t)}"), None, path)
    if t != today:
        return _record(t, ShadowOutcome("skipped_not_today", "섀도는 오늘 날짜로만 돈다(과거·미래 날짜 실행 없음)"),
                       None, path)
    if now.astimezone(KST).time() < MARKET_CLOSE_KST:
        return _record(t, ShadowOutcome("skipped_before_close", f"장 마감({MARKET_CLOSE_KST:%H:%M} KST) 전"), None, path)

    sh = cfg.get("shadow", {})
    ticker = str(sh.get("ticker", "000660"))
    stage_weights = list(sh.get("stage_weights", [1.0, 0.66, 0.33, 0.0]))
    combo_stage = int(sh.get("combo_stage", 3))
    fill = str(cfg.get("fill", engine.DEFAULT_FILL))
    # 종목당 상한은 단계 비중에 먼저 적용한다 — 엔진 목표와 비교 기준 백테스트가 같은 비중을 쓰게(설정에 없으면 끔).
    conc = cfg.get("concentration") or {}
    cap_w = conc.get("max_single_weight")
    raw_stage_weights = list(stage_weights)
    if cap_w is not None and ticker not in set(map(str, conc.get("exempt", []))):
        stage_weights = [min(float(w), float(cap_w)) for w in stage_weights]
    inp = inputs or load_baseline_inputs(ticker)
    ts = pd.Timestamp(t)
    prior = inp.close.index[inp.close.index < ts]
    prev_day = prior[-1] if len(prior) else None
    method = str(sh.get("score_method", wi.DEFAULT_SCORE_METHOD))
    problems = freshness_problems(inp, ts, int(cfg.get("freshness", {}).get("max_foreign_signal_age_days", 4)),
                                  need_open=(fill == "next_open" and prev_day is not None), method=method)
    if problems:
        return _record(t, ShadowOutcome("skipped_stale", "데이터 신선도: " + " / ".join(problems)), None, path)

    try:
        state = model_states(inp, pd.DatetimeIndex([ts]), combo_stage, method).iloc[0]
    except ValueError as e:  # 그룹 방식 기준 기간에 데이터 없음 — 기준점을 지어내지 않는다
        return _record(t, ShadowOutcome("skipped_model", f"위험 점수 계산 불가: {e}"), None, path)
    stage = int(state["stage"])
    weight = float(stage_weights[stage])
    if weight < float(raw_stage_weights[stage]):
        store.append_event(t.isoformat(), SHADOW_ACCOUNT, "concentration_cap",
                           f"{ticker}: 단계 비중 {float(raw_stage_weights[stage]):.0%} → 종목당 상한 {weight:.0%}", path=path)
    out = ShadowOutcome("ok", risk_score=float(state["risk_score"]), stage=stage, target_weight=weight)
    price = float(inp.close[ts])

    runs = store.shadow_runs(path)
    started = runs.loc[runs["status"] == "ok", "decision_date"].min() if not runs.empty else None
    start = pd.Timestamp(started) if isinstance(started, str) else ts
    cap = ladder.get(SHADOW_ACCOUNT).max_capital_krw or 0.0
    if benchmark_close is None:
        benchmark_close = metrics.baseline_benchmarks(cfg["benchmarks"])

    from executor import ladder_ops
    ladder_ops.ensure_initialized(ladder, t.isoformat(), path)  # 가상 자금 배정이 브로커 상태보다 먼저
    if fill == "next_open" and prev_day is not None:
        open_t = float(inp.open[ts])
        # 갭 기록(매일, 주문 유무와 무관): 직전 거래일 종가(결정 시점) vs 오늘 시가(체결 시점)
        store.record_gap(prev_day.date().isoformat(), ticker, float(inp.close[prev_day]), t.isoformat(), open_t, path=path)
        out.gap_pct = (open_t / float(inp.close[prev_day]) - 1) * 100
        # 1) 전 거래일에 결정한 주문을 오늘 시가로 체결(섀도 원장 안). 그보다 오래된 결정은 만료.
        out.fill_result = engine.execute_pending(
            ShadowBroker(ticker, open_t, path), ladder, t.isoformat(), {ticker: open_t},
            valid_decision_dates={prev_day.date().isoformat()}, benchmark_close=benchmark_close, path=path,
            kill_switch_file=ks_file)
    reference = backtest_reference(inp, start, ts, stage_weights, combo_stage, cap, fill, method)
    # 2) 오늘 종가로 결정 — next_open이면 주문은 다음 거래일 시가 체결 대기로 남는다.
    res = engine.run_once(ShadowBroker(ticker, price, path), ladder, t.isoformat(), {ticker: weight}, {ticker: price},
                          strategy=str(sh.get("strategy", "risk_score")), reference_value=reference,
                          benchmark_close=benchmark_close, path=path,
                          cap_rejected_streak=int(cfg["cap_rejected_streak"]), kill_switch_file=ks_file,
                          policy=shadow_policy(), fill=fill,
                          max_weight_gap_pctp=float(cfg.get("max_weight_gap_pctp", 10)),
                          daily_limits=DailyLimits.from_config(cfg.get("daily_limits")) if cfg.get("daily_limits") else None)
    out.engine_result = res
    if res.status != "ok":
        out.status = {"blocked_kill_switch": "blocked_kill_switch", "halt": "blocked_frozen",
                      "review_required": "blocked_frozen"}.get(res.status, "error")
        out.reason = ("킬 스위치로 차단됨: " if out.status == "blocked_kill_switch" else f"엔진 {res.status}: ") + res.message
    for o in res.planned:
        store.insert_shadow_order({"decision_date": t.isoformat(), "ticker": o.ticker, "side": o.side, "qty": o.qty,
                                   "ref_price": o.ref_price, "target_weight": weight,
                                   "risk_score": out.risk_score, "stage": stage}, path=path)
        out.orders.append(o)
    return _record(t, out, res.planning_capital, path)
