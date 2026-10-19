"""
규칙 기반 매수/목표/손절 가격(analysis/price_rules.py)의 과거 성과 측정 +
"화면에 가격을 보여줘도 되는가" 통과 판정.

analysis/pullback.py와 거의 같은 상태기계다. 차이는 손절/익절을 체결가에서
다시 계산하지 않고, **신호 발생일(종가 t)에 이미 정해진 절대 가격**
(entry_price/target_price/stop_price)을 그대로 쓴다는 점이다 — "가격을
미리 계획해두고 그대로 지킨다"는 실제 트레이더의 쓰임에 더 가깝다.

진입 방식(entry_type)은 두 가지다:
  - "limit": 저가가 entry_price까지 내려오면 체결(시가가 이미 그 밑이면
    시가 체결) — "이 가격까지 떨어지면 산다"(ma_pullback·atr_band).
  - "stop": 고가가 entry_price를 넘으면 체결(시가가 이미 넘었으면 시가
    체결) — "이 가격을 넘으면 산다"(breakout_range).
같은 날 target·stop이 동시에 닿으면 손절을 우선한다(보수적 가정,
analysis/pullback.py와 동일한 이유 — 일봉만으로는 장중 순서를 모른다).

**주문을 실행하지 않는다**(CLAUDE.md 원칙 2). 이 모듈은 과거 측정과
통과 판정만 한다 — "오늘의 신호"를 계산하는 것도 이 모듈이 아니라 호출부
(web/main.py)가 레벨 함수의 마지막 행을 그대로 읽는 것뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from analysis import pullback as pb
from analysis import rule_backtest as rbt
from analysis import trading_costs as tcosts

DEFAULT_MAX_HOLD_DAYS = 20

MIN_TRADES_FOR_PASS = 100  # CLAUDE.md 원칙 1(갱신판): 거래 수 100건 미만이면 화면에 가격을 보여주지 않는다.
OUT_OF_SAMPLE_YEARS = 1  # 최근 1년을 검증용(out-of-sample)으로 뗀다.


@dataclass
class PriceRuleTrade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    target_price: float
    stop_price: float
    exit_reason: str  # "target" | "stop" | "max_hold"
    hold_days: int
    return_pct: float  # 거래비용 반영된 라운드트립 수익률(%)


def simulate_price_rule_trades(
    ohlc: pd.DataFrame,
    levels: pd.DataFrame,
    entry_type: str = "limit",
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    commission_rate: float = rbt.DEFAULT_COMMISSION_RATE,
    tax_rate: float = rbt.DEFAULT_TAX_RATE,
    slippage_rate: float = tcosts.DEFAULT_SLIPPAGE_RATE,
    tax_rate_schedule: list[tcosts.RateChange] | None = None,
) -> list[PriceRuleTrade]:
    """
    levels: analysis/price_rules.py의 함수가 반환한
    entry_price/target_price/stop_price DataFrame(ohlc와 같은 인덱스).
    levels[t]는 ohlc[t]의 종가까지로 계산된 레벨이므로, 체결 판정은
    levels[t-1]을 ohlc[t](오늘)에 적용한다 — 미래 참조 금지.

    tax_rate_schedule을 주면 청산일(매도일) 기준 날짜별 세율을 쓰고, 안
    주면 고정 tax_rate를 쓴다(기존 동작 그대로, 하위호환). slippage_rate는
    매수·매도 양쪽에 대칭으로 더해진다(analysis/pullback.py와 동일한 방식).
    """
    if entry_type not in ("limit", "stop"):
        raise ValueError("entry_type은 'limit' 또는 'stop'만 지원합니다.")

    ohlc = ohlc.sort_index()
    levels = levels.reindex(ohlc.index)
    opens, highs, lows, closes = (
        ohlc["open"].to_numpy(), ohlc["high"].to_numpy(),
        ohlc["low"].to_numpy(), ohlc["close"].to_numpy(),
    )
    entry_lv = levels["entry_price"].to_numpy()
    target_lv = levels["target_price"].to_numpy()
    stop_lv = levels["stop_price"].to_numpy()
    dates = ohlc.index

    trades: list[PriceRuleTrade] = []
    position: dict | None = None

    for i in range(1, len(dates)):
        today = dates[i]
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]

        if position is None:
            sig_entry, sig_target, sig_stop = entry_lv[i - 1], target_lv[i - 1], stop_lv[i - 1]
            if sig_entry != sig_entry:  # NaN -> 전날 신호 없음
                continue
            if entry_type == "limit":
                if o <= sig_entry:
                    fill = o
                elif l <= sig_entry:
                    fill = sig_entry
                else:
                    continue
            else:  # "stop" (돌파 매수)
                if o >= sig_entry:
                    fill = o
                elif h >= sig_entry:
                    fill = sig_entry
                else:
                    continue
            position = {
                "entry_date": today, "entry_price": float(fill),
                "target_price": float(sig_target), "stop_price": float(sig_stop),
                "hold_days": 0,
            }
            continue  # 진입 당일은 손절/익절 체크 안 함(하루 단위 단순화)

        position["hold_days"] += 1
        hit_stop = l <= position["stop_price"]
        hit_target = h >= position["target_price"]

        exit_price, exit_reason = None, None
        if hit_stop:  # 둘 다 닿아도 손절 우선(보수적 가정)
            exit_price, exit_reason = position["stop_price"], "stop"
        elif hit_target:
            exit_price, exit_reason = position["target_price"], "target"
        elif position["hold_days"] >= max_hold_days:
            exit_price, exit_reason = float(c), "max_hold"

        if exit_price is not None:
            effective_tax_rate = (
                tcosts.tax_rate_on(today.date(), tax_rate_schedule) if tax_rate_schedule is not None else tax_rate
            )
            buy_cost = position["entry_price"] * (1 + commission_rate + slippage_rate)
            sell_proceeds = exit_price * (1 - commission_rate - effective_tax_rate - slippage_rate)
            trades.append(PriceRuleTrade(
                entry_date=position["entry_date"], exit_date=today,
                entry_price=position["entry_price"], exit_price=float(exit_price),
                target_price=position["target_price"], stop_price=position["stop_price"],
                exit_reason=exit_reason, hold_days=position["hold_days"],
                return_pct=(sell_proceeds / buy_cost - 1) * 100,
            ))
            position = None

    return trades


def summarize_price_rule_trades(
    close: pd.Series,
    trades: list[PriceRuleTrade],
    commission_rate: float = rbt.DEFAULT_COMMISSION_RATE,
    tax_rate: float = rbt.DEFAULT_TAX_RATE,
    out_of_sample_years: int = OUT_OF_SAMPLE_YEARS,
    label: str = "",
) -> dict[str, rbt.RuleBacktestResult]:
    """
    전체 / 검증 제외 기간(최근 out_of_sample_years년 이전) / 최근
    out_of_sample_years년(out-of-sample) 세 구간의 결과를 낸다. 기존
    analysis/rule_backtest.py·pullback.py의 70/30 비율 분할과 다르게
    **달력 기준 최근 N년**으로 뗀다(사용자 요청: "최근 1년은 검증용").
    """
    close = close.sort_index()
    # PriceRuleTrade도 entry_date/exit_date/entry_price/exit_price 속성을
    # 그대로 가지고 있어서 pullback.daily_returns_and_exposure를 그대로 쓸 수 있다.
    ret, eff = pb.daily_returns_and_exposure(close, trades, commission_rate, tax_rate)
    trade_dicts = [{"entry_date": t.entry_date, "return_pct": t.return_pct} for t in trades]

    cutoff = close.index[-1] - pd.DateOffset(years=out_of_sample_years)
    in_idx = close.index[close.index < cutoff]
    out_idx = close.index[close.index >= cutoff]

    return {
        "full": rbt.result_for_window(ret, eff, trade_dicts, f"{label} (전체)".strip()),
        "in_sample": rbt.result_for_window(
            ret.loc[in_idx], eff.loc[in_idx], trade_dicts, f"{label} (검증 제외 기간)".strip()
        ),
        "out_of_sample": rbt.result_for_window(
            ret.loc[out_idx], eff.loc[out_idx], trade_dicts, f"{label} (최근 {out_of_sample_years}년 검증)".strip()
        ),
    }


def backtest_price_rule(
    ohlc: pd.DataFrame,
    levels: pd.DataFrame,
    entry_type: str = "limit",
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    commission_rate: float = rbt.DEFAULT_COMMISSION_RATE,
    tax_rate: float = rbt.DEFAULT_TAX_RATE,
    out_of_sample_years: int = OUT_OF_SAMPLE_YEARS,
    label: str = "",
) -> dict[str, rbt.RuleBacktestResult]:
    """simulate + summarize를 한 번에. 거래 리스트를 따로 또 쓸 일이 없을 때 편의용."""
    trades = simulate_price_rule_trades(
        ohlc, levels, entry_type, max_hold_days, commission_rate, tax_rate,
    )
    return summarize_price_rule_trades(
        ohlc["close"], trades, commission_rate, tax_rate, out_of_sample_years, label,
    )


def passes(results: dict[str, rbt.RuleBacktestResult], min_trades: int = MIN_TRADES_FOR_PASS) -> bool:
    """
    "화면에 매수/목표/손절 기준가를 보여줘도 되는가"의 통과 기준
    (CLAUDE.md 원칙 1 갱신판, 사용자가 명시한 기준 그대로):
      1. 전체 기간 거래 수 >= min_trades(기본 100)
      2. 전체 기간 거래당 평균수익(거래비용 반영)이 양수
      3. 최근 out_of_sample_years년(검증 구간)의 거래당 평균수익도 양수
    세 조건 중 하나라도 거짓이면(거래가 없어 mean_return이 NaN인 경우도
    포함 — NaN > 0은 False라서 자동으로 통과 못 함) False.
    """
    full, oos = results["full"], results["out_of_sample"]
    return bool(full.n >= min_trades and full.mean_return > 0 and oos.mean_return > 0)
