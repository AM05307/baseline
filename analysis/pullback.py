"""
눌림목 지정가 매매 규칙 백테스트.

analysis/rule_backtest.py의 홀딩-시리즈(1=보유/0=현금) 기반 엔진과는 신호의
모양이 다르다. 저긴 "종가 하나로 매일 보유 여부를 정한다"는 전제라 다음날
시가 체결만 알면 됐지만, 여기는:
  - 포지션이 없을 때만 매일 새 지정가 주문을 건다(보유 중엔 주문 자체가 없음)
  - 체결/손절/익절 판정이 그날의 '일봉 구간(저가~고가)'을 봐야 한다
  - 한 번에 1포지션, 보유 기간이 여러 날 이어진다(진입↔청산 사이 매일 손절/
    익절/최대보유일 체크가 필요)
이 전부가 "보유 시리즈 하나 + shift(1)"로는 표현이 안 된다. 그래서 날짜순으로
한 번 훑는 상태기계로 직접 구현했다. 최종 결과 지표(RuleBacktestResult)는
analysis/rule_backtest.py 것을 그대로 재사용한다 — 매매 방식이 달라도
n/승률/MDD/reliable 같은 지표의 모양은 같아야 하고, 같은 모양이어야
scripts/backtest_strategies.py의 표 출력 코드도 그대로 재사용할 수 있다.

전부 순수 함수. DB도 API도 모른다. **주문/매매 실행 코드는 없다**(CLAUDE.md
원칙 2) — 이 모듈은 "이 규칙을 과거에 그대로 적용했다면 어땠을까"를 계산만
한다. 실시간 "오늘의 신호"도 출력하지 않는다.

파라미터(dip_pct 등)는 전부 함수 인자로 노출하되 기본값만 쓴다 — 적은 데이터로
파라미터를 데이터에 맞춰 고르면 과거에만 맞는 우연한 조합을 "전략"으로
착각하기 쉽다(과최적화 방지, analysis/strategies.py와 같은 이유).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import pandas as pd

from analysis import rule_backtest as rbt
from analysis import trading_costs as tcosts

DEFAULT_DIP_PCT = 0.02
DEFAULT_GAP_SKIP_PCT = 0.02
DEFAULT_TAKE_PROFIT_PCT = 0.02
DEFAULT_STOP_LOSS_PCT = 0.03
DEFAULT_MAX_HOLD_DAYS = 10

TREND_LOOKBACK_DAYS = 20
TREND_UP_THRESHOLD_PCT = 8.0
TREND_DOWN_THRESHOLD_PCT = -8.0


@dataclass
class PullbackTrade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    exit_reason: str  # "take_profit" | "stop_loss" | "max_hold"
    hold_days: int
    return_pct: float  # 거래비용 반영된 라운드트립 수익률(%)


@dataclass
class RegimeBreakdown:
    regime: str  # "uptrend" | "box" | "downtrend"
    n: int
    win_rate_pct: float
    mean_return_pct: float  # 거래당 평균손익 = 기대값(expectancy). 전체 거래(승+패) 산술평균.
    median_return_pct: float  # 거래당 중앙값 — 평균은 극단치(최악 거래 하나) 영향을 크게 받으므로 같이 본다.
    worst_pct: float
    total_return_pct: float  # 그 장세로 분류된 거래만 시간순으로 체이닝한 손익(복리)
    mdd_pct: float  # 위 체이닝에서의 최대낙폭 — 수익률과 같은 비중으로 본다(원칙 4)
    buy_and_hold_pct: float  # 같은 장세로 분류된 날짜만 보유했을 때의 수익률(기회비용 비교용)
    reliable: bool
    min_samples: int


def simulate_pullback_trades(
    ohlc: pd.DataFrame,
    dip_pct: float = DEFAULT_DIP_PCT,
    gap_skip_pct: float = DEFAULT_GAP_SKIP_PCT,
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
    stop_loss_pct: float | None = DEFAULT_STOP_LOSS_PCT,
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    commission_rate: float = rbt.DEFAULT_COMMISSION_RATE,
    tax_rate: float = rbt.DEFAULT_TAX_RATE,
    slippage_rate: float = tcosts.DEFAULT_SLIPPAGE_RATE,
    tax_rate_schedule: list[tcosts.RateChange] | None = None,
) -> list[PullbackTrade]:
    """
    날짜순으로 한 번 훑는 상태기계. ohlc는 open/high/low/close 컬럼을 가진
    DataFrame(index=날짜, 정렬돼 있지 않아도 됨 — 내부에서 정렬한다).

    포지션이 없는 날:
        전일 종가 대비 당일 시가가 +gap_skip_pct 이상 갭상승이면 그날은 주문을
        안 낸다(스킵). 아니면 지정가 = 전일 종가 * (1 - dip_pct).
        당일 시가 <= 지정가면 시가에 체결(이미 지정가보다 싸게 열렸으므로).
        아니면 당일 저가 <= 지정가면 지정가 그대로 체결. 둘 다 아니면 그날은
        체결 안 됨 — 다음날 다시 전일(=오늘) 종가 기준으로 새 지정가를 건다.

    포지션이 있는 날(진입 다음날부터 — 진입 당일은 체결만 되고 그날 바로
    손절/익절 체크는 하지 않는다. 하루 단위 단순화, 실제론 동시에 일어날 수
    있지만 일봉만으로는 순서를 알 수 없다):
        익절가 = 진입가 * (1+take_profit_pct), 당일 고가가 닿으면 체결.
        손절가(stop_loss_pct가 None이 아니면) = 진입가 * (1-stop_loss_pct),
        당일 저가가 닿으면 체결. **같은 날 둘 다 닿으면 손절이 먼저 체결된
        것으로 본다** — 보수적 가정. 일봉(시가/고가/저가/종가)만으로는 그날
        장중에 저가와 고가 중 어느 쪽을 먼저 지났는지 알 수 없어서, 유리한
        쪽(익절)을 가정해 결과를 좋게 부풀리지 않기 위해 손절을 우선한다.
        둘 다 안 닿았는데 보유 기간이 max_hold_days에 도달하면 그날 종가에
        청산(마지막 날 강제 청산).

    체결가에는 매수 시 수수료+슬리피지, 매도 시 수수료+세금+슬리피지를
    반영한다(기존 규칙 백테스트 엔진과 동일한 거래비용 적용 방식).
    tax_rate_schedule을 주면 그 청산일(매도일) 기준으로 날짜별 세율을
    조회해서 쓰고, 안 주면 고정 tax_rate를 쓴다(기존 동작 그대로, 하위호환).
    """
    ohlc = ohlc.sort_index()
    opens, highs, lows, closes = (
        ohlc["open"].to_numpy(), ohlc["high"].to_numpy(),
        ohlc["low"].to_numpy(), ohlc["close"].to_numpy(),
    )
    dates = ohlc.index

    trades: list[PullbackTrade] = []
    position: dict | None = None  # {"entry_date","entry_price","hold_days"}

    for i in range(1, len(dates)):
        today = dates[i]
        prev_close = closes[i - 1]
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]

        if position is None:
            if o >= prev_close * (1 + gap_skip_pct):
                continue  # 갭상승 스킵 — 그날은 주문 자체를 안 낸 것으로 본다
            limit_price = prev_close * (1 - dip_pct)
            if o <= limit_price:
                fill_price = o
            elif l <= limit_price:
                fill_price = limit_price
            else:
                continue  # 체결 안 됨
            position = {"entry_date": today, "entry_price": float(fill_price), "hold_days": 0}
            continue  # 진입 당일은 손절/익절 체크 안 함(위 docstring 참고)

        position["hold_days"] += 1
        entry_price = position["entry_price"]
        tp_price = entry_price * (1 + take_profit_pct)
        sl_price = entry_price * (1 - stop_loss_pct) if stop_loss_pct is not None else None

        hit_sl = sl_price is not None and l <= sl_price
        hit_tp = h >= tp_price

        exit_price, exit_reason = None, None
        if hit_sl:  # 둘 다 닿아도 손절 우선(보수적 가정)
            exit_price, exit_reason = sl_price, "stop_loss"
        elif hit_tp:
            exit_price, exit_reason = tp_price, "take_profit"
        elif position["hold_days"] >= max_hold_days:
            exit_price, exit_reason = float(c), "max_hold"

        if exit_price is not None:
            effective_tax_rate = (
                tcosts.tax_rate_on(today.date(), tax_rate_schedule) if tax_rate_schedule is not None else tax_rate
            )
            buy_cost = entry_price * (1 + commission_rate + slippage_rate)
            sell_proceeds = exit_price * (1 - commission_rate - effective_tax_rate - slippage_rate)
            trades.append(PullbackTrade(
                entry_date=position["entry_date"], exit_date=today,
                entry_price=entry_price, exit_price=float(exit_price),
                exit_reason=exit_reason, hold_days=position["hold_days"],
                return_pct=(sell_proceeds / buy_cost - 1) * 100,
            ))
            position = None

    return trades


def daily_returns_and_exposure(
    close: pd.Series, trades: list[PullbackTrade], commission_rate: float, tax_rate: float,
) -> tuple[pd.Series, pd.Series]:
    """
    거래 리스트를 하루 단위 수익률(ret)·실제 보유 상태(eff)로 펼친다 —
    analysis/rule_backtest.py의 entry/hold/exit 일별 처리와 같은 방식이라
    result_for_window()에 그대로 넘길 수 있다. 거래끼리 겹치지 않는다는
    전제(진입 당일엔 손절/익절 체크를 안 하므로 exit_date != 다음 entry_date인
    경우가 보장됨, simulate_pullback_trades 참고)로 단순하게 구현했다.
    """
    close = close.sort_index()
    ret = pd.Series(0.0, index=close.index)
    eff = pd.Series(0, index=close.index)
    prev_close = close.shift(1)

    for t in trades:
        span = close.loc[t.entry_date:t.exit_date].index
        eff.loc[span] = 1
        ret.loc[t.entry_date] = close.loc[t.entry_date] / (t.entry_price * (1 + commission_rate)) - 1
        mid = span[1:-1]
        if len(mid):
            ret.loc[mid] = close.loc[mid] / prev_close.loc[mid] - 1
        ret.loc[t.exit_date] = (t.exit_price * (1 - commission_rate - tax_rate)) / prev_close.loc[t.exit_date] - 1

    return ret, eff


def backtest_pullback(
    ohlc: pd.DataFrame,
    dip_pct: float = DEFAULT_DIP_PCT,
    gap_skip_pct: float = DEFAULT_GAP_SKIP_PCT,
    take_profit_pct: float = DEFAULT_TAKE_PROFIT_PCT,
    stop_loss_pct: float | None = DEFAULT_STOP_LOSS_PCT,
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    commission_rate: float = rbt.DEFAULT_COMMISSION_RATE,
    tax_rate: float = rbt.DEFAULT_TAX_RATE,
    in_sample_frac: float = 0.7,
    label: str = "pullback",
) -> dict[str, rbt.RuleBacktestResult]:
    """
    눌림목 지정가 매매를 시뮬레이션하고, analysis/rule_backtest.py와 같은 모양의
    전체/in-sample(앞 in_sample_frac)/out-of-sample(뒤 나머지) 결과를 낸다.
    """
    trades = simulate_pullback_trades(
        ohlc, dip_pct, gap_skip_pct, take_profit_pct, stop_loss_pct,
        max_hold_days, commission_rate, tax_rate,
    )
    return summarize_pullback_trades(
        ohlc["close"], trades, commission_rate, tax_rate, in_sample_frac, label,
    )


def summarize_pullback_trades(
    close: pd.Series,
    trades: list[PullbackTrade],
    commission_rate: float = rbt.DEFAULT_COMMISSION_RATE,
    tax_rate: float = rbt.DEFAULT_TAX_RATE,
    in_sample_frac: float = 0.7,
    label: str = "pullback",
) -> dict[str, rbt.RuleBacktestResult]:
    """simulate_pullback_trades()가 이미 계산해둔 trades가 있을 때(예: 장세
    구분 리포트와 중복 계산을 피하려고) 그걸 그대로 받아 지표만 낸다."""
    close = close.sort_index()
    ret, eff = daily_returns_and_exposure(close, trades, commission_rate, tax_rate)
    trade_dicts = [{"entry_date": t.entry_date, "return_pct": t.return_pct} for t in trades]

    n = len(close)
    split_pos = int(n * in_sample_frac)
    in_idx = close.index[:split_pos]
    out_idx = close.index[split_pos:]

    return {
        "full": rbt.result_for_window(ret, eff, trade_dicts, f"{label} (전체)".strip()),
        "in_sample": rbt.result_for_window(
            ret.loc[in_idx], eff.loc[in_idx], trade_dicts, f"{label} (in-sample)".strip()
        ),
        "out_of_sample": rbt.result_for_window(
            ret.loc[out_idx], eff.loc[out_idx], trade_dicts, f"{label} (out-of-sample)".strip()
        ),
    }


def classify_regime(
    close: pd.Series,
    lookback: int = TREND_LOOKBACK_DAYS,
    up_threshold_pct: float = TREND_UP_THRESHOLD_PCT,
    down_threshold_pct: float = TREND_DOWN_THRESHOLD_PCT,
) -> pd.Series:
    """
    각 날짜를 '그날까지 직전 lookback거래일 수익률' 기준으로 분류한다:
    up_threshold_pct 초과 = uptrend, down_threshold_pct 미만 = downtrend,
    그 사이 = box. 과거 lookback일만 보므로 미래 참조가 없다 — 분류 대상 날짜
    자신의 종가까지만 쓴다(거래 진입일을 분류하는 용도).

    처음 lookback거래일은 분류 불가라 None.
    """
    close = close.sort_index()
    trailing_return_pct = close.pct_change(lookback) * 100
    regime = pd.Series(None, index=close.index, dtype=object)
    known = trailing_return_pct.notna()
    regime[known & (trailing_return_pct > up_threshold_pct)] = "uptrend"
    regime[known & (trailing_return_pct < down_threshold_pct)] = "downtrend"
    regime[known & (trailing_return_pct >= down_threshold_pct) & (trailing_return_pct <= up_threshold_pct)] = "box"
    return regime


def regime_breakdown(
    trades: list[PullbackTrade],
    close: pd.Series,
    lookback: int = TREND_LOOKBACK_DAYS,
    up_threshold_pct: float = TREND_UP_THRESHOLD_PCT,
    down_threshold_pct: float = TREND_DOWN_THRESHOLD_PCT,
) -> dict[str, RegimeBreakdown]:
    """
    각 거래를 진입일의 장세(classify_regime)로 묶어서 n/승률/평균손익(기대값)/
    중앙값/최악거래/총손익/MDD를 따로 내고, 같은 장세로 분류된 날짜만
    보유했을 때의 buy & hold 수익률도 같이 낸다.

    평균손익(mean_return_pct)·중앙값(median_return_pct)·최악거래(worst_pct)는
    **규칙 자체의 거래당 성적**이다 — buy&hold와는 무관한, "이 규칙으로 거래를
    하나 했을 때 기대할 수 있는 손익"을 보는 지표다(거래비용 반영됨). 평균은
    극단치(아주 큰 손실 하나) 영향을 크게 받을 수 있어서 중앙값을 같이 둔다.
    buy&hold 수익률은 별도로, "이 규칙 대신 그 장세에 단순 보유했다면"이라는
    비교 기준(기회비용)이다 — 둘을 같은 표에 놓긴 하지만 서로 직접 계산에
    쓰이진 않는다.

    총손익/MDD는 그 장세의 거래들을 진입일 순서대로 체이닝(복리)한 결과다 —
    산술 평균이 아니라 실제로 그 거래들만 연속으로 했다면 어땠을지를 본다.
    표본(n) < MIN_SAMPLES(analysis/rule_backtest.MIN_SAMPLES)면 reliable=False.

    buy&hold 구간 주의(겹침 금지): regime[t]는 t일 종가까지의 데이터로 정한
    분류다 — "그 분류를 만든 바로 그날의 수익률"을 그대로 되돌려 집계하면
    분류 기준과 측정 기준이 같은 날이 되어 순환 참조가 생긴다(예: 어떤 날
    +30% 급등 때문에 그날이 '상승추세'로 분류됐는데, 그 +30%를 다시 '상승추세
    buy&hold 수익'으로 집계하면 분류를 만든 수익을 되돌려 세는 꼴). 그래서
    regime[t]에는 t일 종가까지의 데이터를 쓰고, 거기 붙이는 수익률은 **t일→
    t+1일** 수익률을 쓴다(나머지 신호들이 'close[t] 신호 → t+1일 체결/효과'로
    맞추는 것과 같은 원칙).
    """
    close = close.sort_index()
    regime = classify_regime(close, lookback, up_threshold_pct, down_threshold_pct)
    next_day_ret = close.pct_change().shift(-1)  # next_day_ret[t] = close[t+1]/close[t] - 1

    out: dict[str, RegimeBreakdown] = {}
    for label in ("uptrend", "box", "downtrend"):
        label_trades = sorted(
            (t for t in trades if regime.get(t.entry_date) == label),
            key=lambda t: t.entry_date,
        )
        returns = [t.return_pct for t in label_trades]
        n = len(returns)

        if n:
            win_rate_pct = sum(1 for r in returns if r > 0) / n * 100
            mean_return_pct = sum(returns) / n
            median_return_pct = statistics.median(returns)
            worst_pct = min(returns)
            cum, peak, mdd = 1.0, 1.0, 0.0
            for r in returns:
                cum *= 1 + r / 100
                peak = max(peak, cum)
                mdd = min(mdd, cum / peak - 1)
            total_return_pct = (cum - 1) * 100
            mdd_pct = mdd * 100
        else:
            win_rate_pct = mean_return_pct = median_return_pct = worst_pct = float("nan")
            total_return_pct = mdd_pct = float("nan")

        # regime==label인 날의 "다음날" 수익률만 모은다(위 docstring의 겹침 방지).
        # 마지막 날처럼 다음날이 없는 경우는 next_day_ret가 NaN이라 자연히 제외된다.
        mask = (regime == label) & next_day_ret.notna()
        buy_and_hold_pct = (
            float((1 + next_day_ret[mask]).prod() - 1) * 100 if mask.any() else float("nan")
        )

        out[label] = RegimeBreakdown(
            regime=label, n=n, win_rate_pct=win_rate_pct, mean_return_pct=mean_return_pct,
            median_return_pct=median_return_pct, worst_pct=worst_pct,
            total_return_pct=total_return_pct, mdd_pct=mdd_pct,
            buy_and_hold_pct=buy_and_hold_pct, reliable=n >= rbt.MIN_SAMPLES,
            min_samples=rbt.MIN_SAMPLES,
        )
    return out
