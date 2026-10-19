"""
국장/미장 비교 백테스트 — 순수 함수(DB·네트워크 없음). CLI는 scripts/market_compare.py.

모든 계산은 원화 기준이다. 해외 대상은 그날(또는 그 이전 가장 가까운 날)의 실제 USD/KRW 환율로 시가·종가를 원화로 바꾼다.
수량은 소수점(연속)으로 둔다 — 1주 가격이 큰 종목(000660)과 작은 ETF를 같은 조건으로 비교하려는 단순화.
체결: t일 종가로 정한 행동 → t+1일 시가(전략 C·D). 적립·최초 매수(A·B)는 그날 시가에 산다. 현금 이자는 0.
가격 수익률만 본다(배당 미포함 — 저장된 종가가 수정주가가 아니다. 배당이 큰 대상일수록 과소평가).

전략:
  A 단순 보유: 첫날 시가에 전액 매수 후 보유.
  B 매달 적립: 매달 첫 거래일 시가에 같은 금액 매수. 연환산은 돈의 가중(XIRR), MDD·회복 기간은 현금흐름을 뺀 시간가중 지수 기준.
  C 위험점수 비중 조절: 단계(0~3) → 비중(100/66/33/0%). 목표가 바뀔 때만 리밸런싱(analysis/position_policy와 같은 규칙).
  D 고점 대비 하락 분할매수: 첫날 initial_fraction만 사고, 시작 이후 최고 종가 대비 하락률이 levels에 처음 닿은 날 다음 시가에
    나머지를 같은 몫씩 산다(한 번 산 몫은 팔지 않음). 끝까지 안 닿은 몫은 현금으로 남는다.

세금(해외 대상만): 매도로 실현한 원화 기준 이익(환차익 포함)을 해마다 합쳐 공제액을 뺀 뒤 세율을 곱해 그해 마지막 거래일에 현금에서
낸다(실제 납부는 다음 해 5월 — 단순화). 결과는 "기말 평가액"과 "기말 전량 매도 가정 세후"를 같이 보여준다.
국내 대상: 000660은 매도 시 증권거래세(analysis/trading_costs 스케줄), 국내 주식형 ETF(069500)는 매도세·양도세 없음으로 가정.
세법·수수료 값은 config/market_compare.yaml 파라미터이고 최신 세법·증권사 요율 확인이 필요하다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import numpy as np
import pandas as pd

STRATEGIES = ("A", "B", "C", "D")
STRATEGY_LABELS = {"A": "A 단순 보유", "B": "B 매달 적립", "C": "C 위험점수 비중 조절", "D": "D 고점 대비 하락 분할매수"}


@dataclass(frozen=True)
class MarketCosts:
    commission: float  # 매수·매도 편도
    sell_tax_fn: Callable[[date], float]  # 매도 거래세(국내 주식). 없으면 0을 돌려주는 함수
    fx_spread: float = 0.0  # 환전 비용(해외만, 매수·매도 각각)
    cg_tax_rate: float = 0.0  # 해외 양도소득세율(국내 0)
    cg_deduction: float = 0.0  # 연간 기본공제(원)


def to_krw(price: pd.Series, fx: pd.Series | None) -> pd.Series:
    """fx가 없으면 이미 원화. 있으면 각 날짜 이전 가장 가까운 환율(as-of)로 곱한다 — 그날 환율이 없으면 직전 값."""
    if fx is None:
        return price.astype(float)
    f = fx.sort_index().astype(float)
    rate = f.reindex(f.index.union(price.index)).ffill().reindex(price.index)
    return price.astype(float) * rate


@dataclass
class SimResult:
    equity: pd.Series  # 원화 평가액(현금 + 보유 × 종가)
    twr: pd.Series  # 현금흐름을 뺀 시간가중 지수(첫날 1)
    contributions: list[tuple[pd.Timestamp, float]]
    final_value: float
    after_tax_value: float  # 기말 전량 매도(비용·세금) 가정
    taxes_paid: float
    trades: int
    invested_share_end: float  # 끝날 보유 평가액 / 평가액
    notes: list[str] = field(default_factory=list)


def simulate(strategy: str, open_: pd.Series, close: pd.Series, costs: MarketCosts, *, initial: float, monthly: float,
             decided_weight: pd.Series | None = None, dd_initial: float = 0.25,
             dd_levels: tuple[float, ...] = (-0.10, -0.20, -0.30)) -> SimResult:
    """open_/close: 원화 가격(같은 인덱스, 오름차순). decided_weight: C용, t일 종가 결정 비중(인덱스 ⊇ 첫날 직전 결정 포함 가능)."""
    if strategy not in STRATEGIES:
        raise ValueError(strategy)
    idx = close.index
    o, c = open_.to_numpy(float), close.to_numpy(float)
    n = len(idx)
    cash = shares = basis = 0.0
    realized: dict[int, float] = {}
    taxes = 0.0
    trades = 0
    contribs: list[tuple[pd.Timestamp, float]] = []
    equity = np.zeros(n)
    twr = np.ones(n)
    buy_cost = 1 + costs.commission + costs.fx_spread

    def buy(budget: float, px: float) -> None:
        nonlocal cash, shares, basis, trades
        if budget <= 1e-9:
            return
        q = budget / (px * buy_cost)
        shares += q
        basis += budget
        cash -= budget
        trades += 1

    def sell(q: float, px: float, d: pd.Timestamp) -> None:
        nonlocal cash, shares, basis, trades
        if q <= 1e-12:
            return
        q = min(q, shares)
        rate = costs.commission + costs.fx_spread + costs.sell_tax_fn(d.date())
        proceeds = q * px * (1 - rate)
        cost = basis * (q / shares)
        realized[d.year] = realized.get(d.year, 0.0) + proceeds - cost
        basis -= cost
        shares -= q
        cash += proceeds
        trades += 1

    if strategy == "C":
        if decided_weight is None:
            raise ValueError("C는 decided_weight가 필요하다")
        dw = decided_weight.reindex(decided_weight.index.union(idx)).ffill()
        prev_dec = dw[dw.index < idx[0]]
        w_prev = float(prev_dec.iloc[-1]) if len(prev_dec) and pd.notna(prev_dec.iloc[-1]) else 1.0
        dec = dw.reindex(idx).fillna(w_prev).to_numpy(float)
    held_w = None
    peak = -np.inf
    dd_bought = 0
    dd_pending = 0
    last_month = None
    for i, d in enumerate(idx):
        cf = 0.0
        if i == 0:
            cf = initial if strategy != "B" else 0.0
        if strategy == "B" and (last_month is None or (d.year, d.month) != last_month):
            cf += monthly
        last_month = (d.year, d.month)
        if cf:
            cash += cf
            contribs.append((d, cf))
        px = o[i]
        if strategy in ("A", "B"):
            buy(cash, px)
        elif strategy == "C":
            w = w_prev if i == 0 else dec[i - 1]
            if held_w is None or abs(w - held_w) > 1e-12:
                value = cash + shares * px
                want = w * value / px
                if want < shares - 1e-12:
                    sell(shares - want, px, d)
                elif want > shares + 1e-12:
                    buy(min(cash, (want - shares) * px * buy_cost), px)
                held_w = w
        else:  # D
            if i == 0:
                buy(initial * dd_initial, px)
            elif dd_pending:
                tranche = initial * (1 - dd_initial) / len(dd_levels)
                buy(min(cash, tranche * dd_pending), px)
                dd_pending = 0
        # 연말 세금(해외만): 그해 마지막 거래일 종가에 현금에서 낸다
        if costs.cg_tax_rate and (i == n - 1 or idx[i + 1].year != d.year):
            tax = max(0.0, realized.get(d.year, 0.0) - costs.cg_deduction) * costs.cg_tax_rate
            if tax > 0 and i < n - 1:  # 마지막 날은 아래 청산 계산에서 한꺼번에
                if cash < tax:
                    sell((tax - cash) / (c[i] * (1 - costs.commission - costs.fx_spread)), c[i], d)
                cash -= tax
                taxes += tax
                realized[d.year] = 0.0
        equity[i] = cash + shares * c[i]
        if i > 0:
            twr[i] = twr[i - 1] * ((equity[i] - cf) / equity[i - 1]) if equity[i - 1] > 0 else twr[i - 1]
        if strategy == "D":
            peak = max(peak, c[i])
            dd = c[i] / peak - 1
            while dd_bought < len(dd_levels) and dd <= dd_levels[dd_bought] + 1e-12:
                dd_bought += 1
                dd_pending += 1
    # 기말 전량 매도 가정 세후
    last = idx[-1]
    rate = costs.commission + costs.fx_spread + costs.sell_tax_fn(last.date())
    proceeds = shares * c[-1] * (1 - rate)
    gain_last = realized.get(last.year, 0.0) + proceeds - basis
    final_tax = max(0.0, gain_last - costs.cg_deduction) * costs.cg_tax_rate if costs.cg_tax_rate else 0.0
    after_tax = cash + proceeds - final_tax
    notes = []
    if strategy == "D" and dd_bought < len(dd_levels):
        notes.append(f"분할매수 {len(dd_levels) - dd_bought}몫 미집행(현금)")
    return SimResult(pd.Series(equity, index=idx), pd.Series(twr, index=idx), contribs, float(equity[-1]),
                     float(after_tax), float(taxes + final_tax), trades,
                     float(shares * c[-1] / equity[-1]) if equity[-1] > 0 else 0.0, notes)


def max_drawdown(index: pd.Series) -> float:
    return float((index / index.cummax() - 1).min()) if len(index) else float("nan")


def longest_recovery_days(index: pd.Series) -> tuple[int, bool]:
    """고점에서 내려간 뒤 그 고점을 다시 넘기까지 걸린 가장 긴 달력일 수. 끝까지 회복 못 한 구간이 가장 길면 (일수, True)."""
    if index.empty:
        return 0, False
    peak_val, peak_day = index.iloc[0], index.index[0]
    longest, unrecovered = 0, False
    under = False
    for d, v in index.items():
        if v >= peak_val - 1e-12:
            if under:
                days = (d - peak_day).days
                if days > longest:
                    longest, unrecovered = days, False
                under = False
            peak_val, peak_day = v, d
        else:
            under = True
    if under:
        days = (index.index[-1] - peak_day).days
        if days > longest:
            longest, unrecovered = days, True
    return longest, unrecovered


def xirr(cashflows: list[tuple[pd.Timestamp, float]], final_date: pd.Timestamp, final_value: float) -> float:
    """돈의 가중 연환산 수익률. cashflows: (날짜, 납입액>0). 이분법(−99%~+1000%)."""
    flows = [(d, -a) for d, a in cashflows] + [(final_date, final_value)]
    t0 = flows[0][0]
    yrs = np.array([(d - t0).days / 365.25 for d, _ in flows])
    amt = np.array([a for _, a in flows])

    def npv(r: float) -> float:
        return float((amt / (1 + r) ** yrs).sum())

    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


@dataclass
class Metrics:
    cum: float
    cagr: float
    mdd: float
    recovery_days: int
    unrecovered: bool
    after_tax_cum: float
    after_tax_cagr: float
    contributed: float
    final_value: float


def metrics(strategy: str, r: SimResult) -> Metrics:
    idx = r.equity.index
    contributed = sum(a for _, a in r.contributions)
    years = (idx[-1] - idx[0]).days / 365.25
    if strategy == "B":
        cagr = xirr(r.contributions, idx[-1], r.final_value)
        at_cagr = xirr(r.contributions, idx[-1], r.after_tax_value)
    else:
        cagr = (r.final_value / contributed) ** (1 / years) - 1 if years > 0 else float("nan")
        at_cagr = (r.after_tax_value / contributed) ** (1 / years) - 1 if years > 0 else float("nan")
    rec, unrec = longest_recovery_days(r.twr)
    return Metrics(r.final_value / contributed - 1, cagr, max_drawdown(r.twr), rec, unrec,
                   r.after_tax_value / contributed - 1, at_cagr, contributed, r.final_value)
