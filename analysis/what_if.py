"""
가상 비중 조절 백테스트 — "위험 점수 모델대로 비중을 조절했다면 어땠을지".

**주문/매매는 하지 않는다**(원칙 2). 과거 시세에 비중 규칙을 적용해서 수익률을
다시 계산하는 측정일 뿐이다. 이 모듈은 analysis/ 관례대로 순수 함수만 담는다
(DB/API 비의존) — DB에서 읽어 이 모양으로 바꾸는 건 scripts/what_if_recent.py.

**시점 규칙(미래참조 금지)**:
  - t일 종가까지 알 수 있던 정보로 위험 점수·상태를 정하고, 그 상태의 비중을
    t+1일에 적용한다. 리밸런싱은 t일 종가에 체결된다고 가정하고, t+1일 수익률
    (close[t] → close[t+1])에 그 비중이 걸린다.
  - 신호값은 `align_signals()`가 결정일 t 기준으로 정렬한다. 한국 장(KRX) 신호는
    t일 값까지, **한국 장 마감 뒤에 끝나는 해외 세션 신호**(미국 금리·미국 ETF·
    해외 지수 기반)는 t일보다 **엄격히 이전** 값만 쓴다 — 미국 t일 종가는 한국
    t일 15:30 이후에 나온다.
  - 가중치는 `weights_for_dates()`가 recomputed_on <= t인 가장 최근 행만 쓴다.

**비중 변경과 거래비용**: 목표 비중이 바뀌는 날에만 거래한다. 그 사이 주가가
움직여 실제 비중이 목표에서 벗어나는 것(드리프트)은 그대로 두고 매일 맞추지
않는다 — 매일 맞추면 비중 66%/33% 구간에서 의미 없는 소액 거래가 매일 생긴다.
비용은 거래 금액(|목표 주식평가액 - 현재 주식평가액|)에 매수면 수수료+슬리피지,
매도면 수수료+세금+슬리피지를 곱해 자산에서 뺀다. 기간 끝에 청산하지 않는다
(평가 기준) — 청산하면 매도세가 결과에 추가로 붙는다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

from analysis import position_policy as pp
from analysis import risk_score as rs
from analysis import risk_score_history as rsh
from analysis import group_score as gs
from analysis import signal_alignment as sa

# 상태 단계 → 보유 비중(설정값, 사용자 지정 기본값 — 백테스트로 고른 값 아님)
DEFAULT_STAGE_WEIGHTS: dict[int, float] = {0: 1.00, 1: 0.66, 2: 0.33, 3: 0.00}
STAGE_LABELS = {0: "정상", 1: "1단계", 2: "2단계", 3: "3단계"}

# KHJ_COMBO 트리거는 risk_score.determine_status()가 점수와 무관하게 "분할 매도
# 단계 (KHJ_COMBO)"로 덮어쓰는 가장 강한 상태라, 기본은 3단계로 본다.
# determine_status()에는 점수로 도달하는 3/3 단계가 없다(1/3·2/3만) — 그래서
# 3단계는 combo로만 도달한다. 다르게 보려면 --combo-stage로 바꾼다.
DEFAULT_COMBO_STAGE = 3

MAX_SIGNAL_GAP_DAYS = sa.MAX_SIGNAL_GAP_DAYS


def stage_from_status(sell_stage: str | None, combo_stage: int = DEFAULT_COMBO_STAGE) -> int:
    """risk_score.determine_status()가 돌려준 "보유 중이라면" 문구를 단계 숫자로."""
    if sell_stage is None:
        return 0
    if "KHJ_COMBO" in sell_stage:
        return combo_stage
    if "1/3" in sell_stage:
        return 1
    if "2/3" in sell_stage:
        return 2
    if "3/3" in sell_stage:  # 그룹 방식(analysis/group_score)은 점수 백분위로 3단계에 도달한다
        return 3
    raise ValueError(f"알 수 없는 상태 문구: {sell_stage}")


def align_signals(
    values_by_signal: dict[str, pd.DataFrame],
    decision_dates: pd.DatetimeIndex,
    prior_session_signals: frozenset[str] | set[str] = sa.PRIOR_SESSION_SIGNALS,
    max_gap_days: int = sa.MAX_SIGNAL_GAP_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """analysis/signal_alignment.align_signals()를 그대로 쓴다(공통 규칙 — 그쪽 docstring 참고)."""
    return sa.align_signals(values_by_signal, decision_dates, prior_session_signals, max_gap_days)


def weights_for_dates(
    weight_history: pd.DataFrame, decision_dates: pd.DatetimeIndex, signal_ids: list[str],
) -> pd.DataFrame:
    """
    weight_history: columns = recomputed_on, signal_id, new_weight_pct (+ 선택: id).
    결정일 t마다 recomputed_on <= t인 가장 최근 가중치. 같은 날 같은 신호에
    여러 행이 있으면 algorithm_version이 큰 행, 그다음 id가 큰 행을 쓴다
    (risk_score_history.select_weight_rows). 기록이 아직 없는 날은 NaN(냉시작 — 호출부가 걸러낸다).
    """
    out = pd.DataFrame(np.nan, index=pd.DatetimeIndex(decision_dates).as_unit("ns"), columns=signal_ids, dtype=float)
    if weight_history.empty:
        return out
    wh = weight_history.copy()
    wh["recomputed_on"] = pd.to_datetime(wh["recomputed_on"]).dt.as_unit("ns")
    wh = rsh.select_weight_rows(wh)
    left = pd.DataFrame({"t": out.index.sort_values()})
    for sid in signal_ids:
        sub = wh.loc[wh["signal_id"] == sid, ["recomputed_on", "new_weight_pct"]].rename(
            columns={"recomputed_on": "t", "new_weight_pct": "w"})
        if sub.empty:
            continue
        merged = pd.merge_asof(left, sub, on="t", direction="backward").set_index("t")
        out[sid] = merged["w"].astype(float).reindex(out.index)
    return out


def score_and_stage(
    is_on: pd.DataFrame,
    weights: pd.DataFrame,
    directions: dict[str, str],
    combo_value: pd.Series | None = None,
    risk_threshold: float = rs.DEFAULT_RISK_THRESHOLD,
    sell_stage_2_threshold: float = rs.DEFAULT_SELL_STAGE_2_THRESHOLD,
    combo_stage: int = DEFAULT_COMBO_STAGE,
    net_opportunity: bool = False,
) -> pd.DataFrame:
    """
    결정일별 위험 점수·기회 점수·상태·단계. net_opportunity=True면 예전 방식(opportunity가
    위험 점수를 깎음) — 분리 전/후 비교용. 점수·상태 문구는 risk_score.compute_risk_score()·
    determine_status()를 그대로 호출해서 대시보드와 같은 공식을 쓴다.
    is_on이 NaN(그날 값 없음)인 신호는 꺼진 것으로 계산한다 — 대신 호출부가
    날짜별 신호 커버리지를 따로 보여준다(숨기지 않음).
    """
    rows = []
    for t in is_on.index:
        on = is_on.loc[t]
        w = weights.loc[t]
        on_weights = {sid: float(w[sid]) for sid in is_on.columns
                      if on[sid] == 1 and sid in w.index and pd.notna(w[sid])}
        score, risk_w, opp_w = rs.compute_risk_score(on_weights, directions, net_opportunity)
        cv = combo_value.get(t) if combo_value is not None else None
        combo = bool(cv is not None and pd.notna(cv) and cv >= 3)
        new_buy, sell_stage = rs.determine_status(score, combo, risk_threshold, sell_stage_2_threshold)
        stage = stage_from_status(sell_stage, combo_stage)
        rows.append({
            "date": t, "risk_score": score, "opportunity_score": rs.compute_opportunity_score(opp_w),
            "new_buy_status": new_buy,
            "sell_stage": sell_stage, "stage": stage, "on_signals": sorted(on_weights),
        })
    return pd.DataFrame(rows).set_index("date")


SCORE_METHODS = ("group", "weighted")
DEFAULT_SCORE_METHOD = "group"  # 2026-10-19 사용자 결정: 신호 그룹 균등 가중치 + 백분위 단계. "weighted" = 예전 적중률 가중치


def group_states(is_on: pd.DataFrame, decision_dates: pd.DatetimeIndex, thresholds: gs.Thresholds | None = None,
                 exclude: frozenset[str] | set[str] = frozenset(),
                 calibration: tuple[str, str] = (gs.CALIBRATION_START, gs.CALIBRATION_END)) -> pd.DataFrame:
    """
    그룹 방식 결정일별 상태. is_on은 기준 기간과 결정일을 모두 덮어야 한다(기준점을 그 안에서 정함 — thresholds를 주면 그대로 씀).
    exclude: 대상에 맞지 않아 빼는 신호(예: 지수·해외 ETF에 000660 가격 신호). 반환 DataFrame.attrs["thresholds"]에 기준점.
    """
    scores = gs.risk_score_series(is_on, exclude)
    thr = thresholds or gs.calibrate_thresholds(scores, *calibration)
    stage = gs.stage_series(scores, thr)
    opp = gs.opportunity_score_series(is_on, exclude)
    cov = gs.coverage(is_on, exclude)
    dates = pd.DatetimeIndex(decision_dates).as_unit("ns")
    rows = []
    for t in dates:
        st = int(stage.get(t, 0))
        new_buy, sell = gs.stage_status(st)
        on = is_on.loc[t] if t in is_on.index else pd.Series(dtype=float)
        rows.append({"date": t, "risk_score": float(scores.get(t, np.nan)), "opportunity_score": float(opp.get(t, np.nan)),
                     "new_buy_status": new_buy, "sell_stage": sell, "stage": st, "groups_with_data": int(cov.get(t, 0)),
                     "on_signals": sorted(s for s in on.index if on[s] == 1 and s not in exclude)})
    out = pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame(
        columns=["risk_score", "opportunity_score", "new_buy_status", "sell_stage", "stage", "groups_with_data", "on_signals"])
    out.attrs["thresholds"] = thr
    return out


@dataclass
class CostModel:
    commission_rate: float
    slippage_rate: float
    tax_rate_fn: Callable[[date], float]

    def rate(self, delta: float, trade_date: pd.Timestamp) -> float:
        base = self.commission_rate + self.slippage_rate
        return base + self.tax_rate_fn(trade_date.date()) if delta < 0 else base


def simulate(
    returns: pd.Series,
    target_weight: pd.Series,
    trade_dates: pd.Series,
    initial_weight: float,
    costs: CostModel,
) -> pd.DataFrame:
    """
    returns[t]: t일 종목 수익률(close[t-1]→close[t]).
    target_weight[t]: t일에 적용할 목표 비중(t-1일 종가에 결정·체결).
    trade_dates[t]: 그 체결이 일어난 날(= t-1 거래일) — 세율 조회용.
    initial_weight: 기간 시작 전 보유 비중.

    반환 DataFrame(index=returns.index): weight(목표), traded(거래 금액, 자산
    대비), cost(비용, 자산 대비), changed(비중 변경 여부), ret(포트폴리오 일간
    수익률, 비용 포함), equity(시작 1.0 기준 자산).
    """
    equity, stock = 1.0, initial_weight
    prev_target = initial_weight
    rows = []
    for t in returns.index:
        w = float(target_weight[t])
        r = float(returns[t])
        start_equity = equity
        traded = cost = 0.0
        changed = pp.weight_changed(w, prev_target)  # 드리프트는 맞추지 않음 — executor와 같은 규칙(position_policy)
        if changed:
            delta = w * equity - stock
            traded = abs(delta)
            cost = traded * costs.rate(delta, pd.Timestamp(trade_dates[t]))
            equity -= cost
            stock = w * equity
            prev_target = w
        cash = equity - stock
        stock *= 1 + r
        equity = stock + cash
        rows.append({
            "date": t, "weight": w, "changed": changed,
            "traded": traded / start_equity, "cost": cost / start_equity,
            "ret": equity / start_equity - 1, "equity": equity,
        })
    return pd.DataFrame(rows).set_index("date")


FILL_MODES = ("next_open", "same_close")
DEFAULT_FILL = "next_open"  # 2026-10-19 사용자 결정: t일 종가로 결정 → t+1일 시가 체결. same_close는 옵션으로 남김


def simulate_next_open(
    close: pd.Series,
    open_: pd.Series,
    decided_weight: pd.Series,
    initial_weight: float,
    costs: CostModel,
) -> pd.DataFrame:
    """
    비율(소수점) 백테스트 — **t일 종가에 결정한 비중을 t+1일 시가에 체결**한다.

    close·open_·decided_weight의 index는 [직전 거래일, 기간 첫날, …, 마지막 날]. 첫 원소(직전 거래일)는 결정만
    쓰고(그날 종가에 정한 비중이 기간 첫날 시가에 체결) 결과 행은 만들지 않는다.
    하루: 전일 종가→당일 시가(갭)는 **이전 비중**으로, 시가에 목표 비중으로 바꾼 뒤(목표가 바뀐 경우만, 드리프트는
    맞추지 않음 — simulate와 같은 규칙) 시가→종가는 새 비중으로 움직인다. 비용은 시가 체결액에, 세율은 체결일 기준.

    반환 DataFrame(index=기간 날짜): weight(그날 시가 이후 적용 비중), changed, traded·cost(자산 대비), ret, equity,
    gap_pct(전일 종가 대비 당일 시가 %).
    """
    idx = close.index
    equity, stock = 1.0, float(initial_weight)
    current = float(initial_weight)
    pending = float(decided_weight[idx[0]])
    prev_close = float(close[idx[0]])
    rows = []
    for t in idx[1:]:
        o, c = float(open_[t]), float(close[t])
        start_equity = equity
        cash = equity - stock
        stock *= o / prev_close
        equity_open = stock + cash
        traded = cost = 0.0
        changed = pp.weight_changed(pending, current)
        if changed:
            delta = pending * equity_open - stock
            traded = abs(delta)
            cost = traded * costs.rate(delta, pd.Timestamp(t))
            equity_open -= cost
            stock = pending * equity_open
            current = pending
        cash = equity_open - stock
        stock *= c / o
        equity = stock + cash
        rows.append({"date": t, "weight": current, "changed": changed, "traded": traded / start_equity,
                     "cost": cost / start_equity, "ret": equity / start_equity - 1, "equity": equity,
                     "gap_pct": (o / prev_close - 1) * 100})
        pending = float(decided_weight[t])
        prev_close = c
    return pd.DataFrame(rows).set_index("date")


def simulate_shares(
    close: pd.Series,
    target_weight: pd.Series,
    capital_cap: float,
    policy: pp.PositionPolicy,
    commission_rate: float,
    tax_rate_fn: Callable[[date], float],
    slippage_rate: float = 0.0,
    initial_cash: float | None = None,
    fill: str = DEFAULT_FILL,
    open_: pd.Series | None = None,
) -> pd.DataFrame:
    """
    정수 주식(policy.lot_size) 백테스트 — executor 엔진과 **같은 순서·같은 원장 계산**을 한다(섀도 비교 기준용).

    target_weight[t]: t일 종가에 결정하는 목표 비중. 매일 종가에: 운용분 = 현금 + 수량×종가[t] → 운용 자본 =
    min(운용분, capital_cap)(executor.tiers.operating_capital과 같음) → position_policy.target_quantity로 목표 수량.
      - fill="next_open"(기본): 그 수량 주문을 **t+1일 시가**에 체결한다. 시가에 매수할 현금이 모자라면 살 수 있는
        수량까지만 산다(executor.engine.execute_pending과 같은 규칙). 수량은 종가로 정하고 가격은 시가로 치른다 —
        실제로도 장 시작 전에 수량을 정해 내기 때문이다. 목표 수량이 지금과 같으면 결정 즉시 그 목표를 "체결 완료"로 본다.
      - fill="same_close": 결정한 날 종가에 바로 체결(2026-10-19 이전 기본).
    매수는 현금에서 체결액×(1+수수료+슬리피지), 매도는 체결액×(1−수수료−세금−슬리피지)를 현금에 더한다
    (executor.engine 원장과 같은 식). 부분 체결·지연은 실행 쪽 괴리로 남는다.

    반환 DataFrame(index=close.index): weight(체결 완료 목표), qty, traded(체결액, 원), cost(원), cash, equity(원, 종가 평가).
    """
    if fill not in FILL_MODES:
        raise ValueError(f"fill은 {FILL_MODES} 중 하나: {fill}")
    if fill == "next_open" and open_ is None:
        raise ValueError("fill='next_open'에는 시가(open_)가 필요합니다")
    cash = float(capital_cap if initial_cash is None else initial_cash)
    qty, last_target = 0.0, None
    pending: tuple[float, float] | None = None  # (목표 수량, 목표 비중) — 다음 날 시가에 체결
    rows = []

    def _trade(new_q: float, price: float, t) -> tuple[float, float]:
        nonlocal cash, qty
        delta = new_q - qty
        gross = abs(delta) * price
        cost = 0.0
        if delta > 0:
            cost = gross * (commission_rate + slippage_rate)
            cash -= gross + cost
        elif delta < 0:
            cost = gross * (commission_rate + tax_rate_fn(pd.Timestamp(t).date()) + slippage_rate)
            cash += gross - cost
        qty = new_q
        return gross, cost

    for t in close.index:
        price, w = float(close[t]), float(target_weight[t])
        gross = cost = 0.0
        if fill == "next_open":
            if pending is not None:
                o = float(open_[t])
                want_q, want_w = pending
                if want_q > qty and policy.lot_size:
                    unit = o * (1 + commission_rate + slippage_rate)
                    affordable = qty + math.floor(max(0.0, cash) / unit / policy.lot_size + 1e-9) * policy.lot_size
                    want_q = min(want_q, affordable)
                gross, cost = _trade(want_q, o, t)
                last_target, pending = want_w, None
            capital = max(0.0, min(cash + qty * price, float(capital_cap)))
            new_q = pp.target_quantity(qty, w, last_target, price, capital, policy)
            if new_q != qty:
                pending = (new_q, w)
            else:
                last_target = w
        else:
            capital = max(0.0, min(cash + qty * price, float(capital_cap)))
            new_q = pp.target_quantity(qty, w, last_target, price, capital, policy)
            gross, cost = _trade(new_q, price, t)
            last_target = w
        rows.append({"date": t, "weight": last_target, "qty": qty, "traded": gross, "cost": cost, "cash": cash,
                     "equity": cash + qty * price})
    return pd.DataFrame(rows).set_index("date")


def max_drawdown(equity: pd.Series, start: float = 1.0) -> float:
    """시작값 포함 자산곡선의 최대낙폭(음수 또는 0, 비율)."""
    curve = pd.concat([pd.Series([start]), equity.reset_index(drop=True)])
    return float((curve / curve.cummax() - 1).min())


@dataclass
class WindowSummary:
    cum_return: float
    mdd: float
    total_cost: float
    n_changes: int


def summarize(sim: pd.DataFrame) -> WindowSummary:
    return WindowSummary(
        cum_return=float(sim["equity"].iloc[-1] - 1) if len(sim) else 0.0,
        mdd=max_drawdown(sim["equity"]) if len(sim) else 0.0,
        total_cost=float(sim["cost"].sum()),
        n_changes=int(sim["changed"].sum()),
    )


def rolling_window_diffs(
    returns: pd.Series,
    target_weight: pd.Series,
    trade_dates: pd.Series,
    eligible: pd.Series,
    window: int,
    costs: CostModel,
    initial_weight: float = 1.0,
) -> pd.DataFrame:
    """
    모든 window거래일 구간(한 거래일씩 밀며, 서로 겹침)에서 A(모델)·B(100% 보유)
    누적 수익률과 그 차이 A−B. eligible[t]가 구간 내 모든 날 True인 구간만 쓴다
    (예: 가중치가 weight_history에서 온 날만). 각 구간은 initial_weight에서
    출발한다 — 최근 기간 계산과 같은 조건.
    """
    idx = returns.index
    ones = pd.Series(1.0, index=idx)
    out = []
    elig = eligible.reindex(idx).fillna(False).to_numpy()
    for i in range(0, len(idx) - window + 1):
        if not elig[i:i + window].all():
            continue
        sl = idx[i:i + window]
        a = simulate(returns[sl], target_weight[sl], trade_dates[sl], initial_weight, costs)
        b = simulate(returns[sl], ones[sl], trade_dates[sl], 1.0, costs)
        a_cum, b_cum = a["equity"].iloc[-1] - 1, b["equity"].iloc[-1] - 1
        out.append({"start": sl[0], "end": sl[-1], "a": a_cum, "b": b_cum, "diff": a_cum - b_cum})
    return pd.DataFrame(out, columns=["start", "end", "a", "b", "diff"])


def rolling_window_diffs_next_open(
    close: pd.Series,
    open_: pd.Series,
    decided_weight: pd.Series,
    eligible_decision: pd.Series,
    window: int,
    costs: CostModel,
    initial_weight: float = 1.0,
    end_before: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    rolling_window_diffs의 next_open 판: window거래일 구간마다 [직전 거래일 결정 → 구간 첫날 시가 체결 …]로
    simulate_next_open을 돌려 A(모델)·B(100% 보유)와 A−B를 낸다. 구간 안 모든 날의 비중을 정한 결정일
    (직전 거래일 ~ 마지막 날 전날)이 eligible_decision이어야 하고, 구간 마지막 날이 end_before보다 앞서야 한다.
    """
    idx = close.index
    elig = eligible_decision.reindex(idx).fillna(False).to_numpy()
    out = []
    for i in range(1, len(idx) - window + 1):
        last = idx[i + window - 1]
        if end_before is not None and last >= end_before:
            break
        if not elig[i - 1:i + window - 1].all():
            continue
        rng = idx[i - 1:i + window]
        a = simulate_next_open(close[rng], open_[rng], decided_weight[rng], initial_weight, costs)
        b = simulate_next_open(close[rng], open_[rng], pd.Series(1.0, index=rng), 1.0, costs)
        a_cum, b_cum = a["equity"].iloc[-1] - 1, b["equity"].iloc[-1] - 1
        out.append({"start": rng[1], "end": last, "a": a_cum, "b": b_cum, "diff": a_cum - b_cum})
    return pd.DataFrame(out, columns=["start", "end", "a", "b", "diff"])


def percentile_of(value: float, dist: pd.Series) -> float:
    """dist 중 value보다 작은 비율 + 같은 값의 절반(%) — 동률이 많아도 중간에 놓인다."""
    arr = np.asarray(dist, dtype=float)
    if arr.size == 0:
        return float("nan")
    less = (arr < value - 1e-12).sum()
    equal = (np.abs(arr - value) <= 1e-12).sum()
    return float((less + 0.5 * equal) / arr.size * 100)


_EVIDENCE_RE = re.compile(r"evidence_signals=(\d+)/(\d+)")
_LIVE_RE = re.compile(r"in_sample n_events=(\d+).*?base_rate=([\d.]+)%.*?ci_lower=([\d.]+)%")


def reason_evidence(reason: str | None, min_events: int = 30) -> bool | None:
    """
    weight_history.reason 한 줄로 "데이터 근거로 정해진 가중치인가"를 판별한다.
      - 백필: 'evidence_signals=K/N' → K>0
      - 실시간(신 공식): 'in_sample n_events=N ... base_rate=B% ci_lower=C%' → N>=min_events and C>B
        (weighting.raw_score_significant가 가중치를 주는 조건)
      - 그 밖(구 공식·'no signal history yet' 등): None(판별 불가)
    """
    reason = reason or ""
    m = _EVIDENCE_RE.search(reason)
    if m:
        return int(m.group(1)) > 0
    m = _LIVE_RE.search(reason)
    if m:
        return int(m.group(1)) >= min_events and float(m.group(3)) > float(m.group(2))
    return None


def snapshot_evidence(weight_history: pd.DataFrame, decision_dates: pd.DatetimeIndex) -> pd.Series:
    """
    결정일마다 그때 적용되던 가중치 스냅샷(recomputed_on <= t 중 가장 최근 날짜, 같은 날 여러
    행이면 select_weight_rows 규칙)이 데이터 근거였는지. 스냅샷 안에 근거 있는 신호가 하나라도
    있으면 True, 판별된 행이 전부 근거 없음이면 False(균등 초기값 규칙), 판별 불가면 None.
    """
    out = pd.Series(None, index=pd.DatetimeIndex(decision_dates).as_unit("ns"), dtype=object)
    if weight_history.empty or "reason" not in weight_history.columns:
        return out
    wh = weight_history.copy()
    wh["recomputed_on"] = pd.to_datetime(wh["recomputed_on"]).dt.as_unit("ns")
    wh = rsh.select_weight_rows(wh)
    flags = []
    for d, grp in wh.groupby("recomputed_on"):
        f = [reason_evidence(r) for r in grp["reason"]]
        flags.append({"date": d, "flag": True if any(x is True for x in f) else (False if any(x is False for x in f) else None)})
    snap = pd.DataFrame(flags).sort_values("date")
    left = pd.DataFrame({"date": out.index.sort_values()})
    merged = pd.merge_asof(left, snap, on="date", direction="backward").set_index("date")["flag"]
    return pd.Series([v if v is True or v is False else None for v in merged.reindex(out.index)],
                     index=out.index, dtype=object)


@dataclass
class DistSummary:
    label: str
    n: int
    n_independent: int  # 겹치지 않게 셌을 때 대략의 구간 수(n // window)
    q05: float
    q25: float
    q50: float
    q75: float
    q95: float
    max: float
    percentile: float  # 이번 값의 백분위(%)


def summarize_distribution(label: str, diffs: pd.Series, value: float, window: int) -> DistSummary:
    d = pd.Series(diffs, dtype=float)
    if d.empty:
        nan = float("nan")
        return DistSummary(label, 0, 0, nan, nan, nan, nan, nan, nan, nan)
    q = d.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
    return DistSummary(label, len(d), len(d) // window, q[0.05], q[0.25], q[0.5], q[0.75], q[0.95],
                       float(d.max()), percentile_of(value, d))
