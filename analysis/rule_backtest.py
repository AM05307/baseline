"""
기술적 지표 규칙 백테스트 엔진.

analysis/backtest.py(이벤트 백테스트)와 다른 파일로 둔 이유: 저긴 "이벤트 D일
전후 수익률"을 단순 조회하는 코드라 체결 시점 문제가 없다. 여기는 매일 신호가
바뀔 수 있어서 "미래 데이터 참조 금지"를 엔진 안에서 직접 보장해야 한다.

핵심 규칙 (반드시 지킬 것 — tests/test_strategies.py가 이걸 검증한다):
    t일 종가로 계산한 신호는 t+1일 '시가'에 체결된다. 시가가 없으면 t+1일
    종가로 체결된다. t일 신호가 t일 가격에 영향을 주는 경우는 없다.

구현 방식: strategies.py가 돌려주는 holding(그 날 종가 기준 목표 상태)을
하루 뒤로 밀어서(shift(1)) "그 날 실제로 들고 있던 상태"로 바꾼다. 포지션이
바뀌는 날(진입/청산)만 체결가(시가, 없으면 종가)를 쓰고, 포지션을 유지하는
날은 평범한 종가 대비 종가 수익률을 쓴다. 이렇게 하면 "내일 체결"과 "보유 중
일반 수익률"을 모두 하루 단위로 정확히 표현할 수 있다.

거래비용: 매수/매도 수수료율 + 매도 시 세금률. 아래 기본값은 참고용 근사치다.
    실제 요율은 증권사·연도별로 다르니 실제 사용 전 확인 필요.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

MIN_SAMPLES = 30  # 원칙 3. 다른 analysis 모듈과 같은 기준값이지만 모듈 간 의존은 안 만든다(기존 관행).

DEFAULT_COMMISSION_RATE = 0.00015  # 편도 0.015% (참고용 근사치)
DEFAULT_TAX_RATE = 0.0020  # 매도 시 0.20% (참고용 근사치)


@dataclass
class RuleBacktestResult:
    label: str
    trades: list[float] = field(default_factory=list)  # 완료된 라운드트립 수익률(%)만. 기간 끝에 포지션이 열려 있으면 미포함(미실현이라 체결가가 없어서)
    total_return_pct: float = float("nan")
    cagr_pct: float = float("nan")
    mdd_pct: float = float("nan")  # 최대낙폭. 음수.
    exposure_pct: float = float("nan")  # 보유 기간 비율
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return float("nan")
        return sum(1 for t in self.trades if t > 0) / self.n * 100

    @property
    def mean_return(self) -> float:
        """거래당 평균(승+패 전부 포함). avg_win/avg_loss는 승/패를 나눠 보지만,
        "거래당 평균이 플러스냐"(기대값) 같은 판정엔 이 전체 평균이 필요하다
        (analysis/price_rule_backtest.py의 통과 기준이 이걸 쓴다)."""
        return sum(self.trades) / self.n if self.trades else float("nan")

    @property
    def avg_win(self) -> float:
        wins = [t for t in self.trades if t > 0]
        return sum(wins) / len(wins) if wins else float("nan")

    @property
    def avg_loss(self) -> float:
        losses = [t for t in self.trades if t <= 0]
        return sum(losses) / len(losses) if losses else float("nan")

    @property
    def worst(self) -> float:
        return min(self.trades) if self.trades else float("nan")

    @property
    def best(self) -> float:
        return max(self.trades) if self.trades else float("nan")

    @property
    def reliable(self) -> bool:
        return self.n >= MIN_SAMPLES

    def report(self) -> str:
        """analysis/backtest.py의 관례를 따른다: 최대손실을 승률과 같은 비중으로."""
        lines = [
            f"백테스트: {self.label}",
            "─" * 42,
            f"기간        {self.start} ~ {self.end}",
            f"거래 횟수   {self.n}회",
            f"노출도      {self.exposure_pct:.1f}% (보유 기간 비율)",
            f"총수익률    {self.total_return_pct:+.2f}%",
            f"연환산수익  {self.cagr_pct:+.2f}%",
            f"최대낙폭    {self.mdd_pct:+.2f}%",
            f"승률        {self.win_rate:.1f}%",
            f"평균수익    {self.avg_win:+.2f}%",
            f"평균손실    {self.avg_loss:+.2f}%",
            f"최악거래    {self.worst:+.2f}%",
        ]
        if not self.reliable:
            lines += [
                "",
                f"⚠ 거래 {self.n}회는 통계적 결론을 내기에 부족합니다.",
                f"  (권장 {MIN_SAMPLES}회 이상). 우연일 가능성을 배제할 수 없습니다.",
            ]
        return "\n".join(lines)


def _exec_price(close: pd.Series, open_: pd.Series | None) -> pd.Series:
    """체결가: 그 날 시가. 시가가 없으면(None 또는 NaN) 그 날 종가."""
    if open_ is None:
        return close.copy()
    ep = open_.reindex(close.index)
    return ep.where(ep.notna(), close)


def _simulate(
    close: pd.Series,
    holding: pd.Series,
    open_: pd.Series | None,
    commission_rate: float,
    tax_rate: float,
) -> tuple[pd.Series, pd.Series, list[dict]]:
    """
    일별 수익률(ret), 실제 보유 상태(eff), 완료된 라운드트립 거래 리스트를 계산한다.

    eff(effective holding) = holding.shift(1): "t일 종가로 만든 신호"가 적용되는
    건 t+1일부터다. 이 shift가 미래 참조 금지의 핵심 — t일의 ret/eff는 t일
    이전 데이터로만 결정된다.
    """
    close = close.sort_index()
    holding = holding.reindex(close.index).fillna(0)
    exec_price = _exec_price(close, open_.sort_index() if open_ is not None else None)

    eff = holding.shift(1).fillna(0)
    eff_prev = eff.shift(1, fill_value=0)
    prev_close = close.shift(1)

    entry_mask = (eff == 1) & (eff_prev == 0)
    exit_mask = (eff == 0) & (eff_prev == 1)
    hold_mask = (eff == 1) & (eff_prev == 1)

    ret = pd.Series(0.0, index=close.index)
    ret[hold_mask] = close[hold_mask] / prev_close[hold_mask] - 1
    ret[entry_mask] = close[entry_mask] / (exec_price[entry_mask] * (1 + commission_rate)) - 1
    ret[exit_mask] = (exec_price[exit_mask] * (1 - commission_rate - tax_rate)) / prev_close[exit_mask] - 1

    entry_dates = list(close.index[entry_mask])
    exit_dates = list(close.index[exit_mask])
    trades = []
    for i, entry_date in enumerate(entry_dates):
        if i >= len(exit_dates):
            break  # 기간 끝까지 포지션이 열려 있음 — 체결가가 없어 완료된 거래로 안 센다
        exit_date = exit_dates[i]
        entry_px = exec_price.loc[entry_date] * (1 + commission_rate)
        exit_px = exec_price.loc[exit_date] * (1 - commission_rate - tax_rate)
        trades.append({
            "entry_date": entry_date,
            "exit_date": exit_date,
            "return_pct": (exit_px / entry_px - 1) * 100,
        })

    return ret, eff, trades


def result_for_window(
    ret: pd.Series, eff: pd.Series, trades: list[dict], label: str
) -> RuleBacktestResult:
    if len(ret) == 0:
        return RuleBacktestResult(label=label)

    equity = (1 + ret).cumprod()
    total_return_pct = (equity.iloc[-1] - 1) * 100
    days = (ret.index[-1] - ret.index[0]).days
    years = days / 365.25
    if years > 0 and equity.iloc[-1] > 0:
        cagr_pct = (equity.iloc[-1] ** (1 / years) - 1) * 100
    else:
        cagr_pct = float("nan")
    mdd_pct = (equity / equity.cummax() - 1).min() * 100
    exposure_pct = eff.mean() * 100

    window_trades = [
        t["return_pct"] for t in trades if ret.index[0] <= t["entry_date"] <= ret.index[-1]
    ]

    return RuleBacktestResult(
        label=label,
        trades=window_trades,
        total_return_pct=float(total_return_pct),
        cagr_pct=float(cagr_pct),
        mdd_pct=float(mdd_pct),
        exposure_pct=float(exposure_pct),
        start=ret.index[0].date(),
        end=ret.index[-1].date(),
    )


def backtest_rule(
    close: pd.Series,
    holding: pd.Series,
    open_: pd.Series | None = None,
    commission_rate: float = DEFAULT_COMMISSION_RATE,
    tax_rate: float = DEFAULT_TAX_RATE,
    in_sample_frac: float = 0.7,
    label: str = "",
) -> dict[str, RuleBacktestResult]:
    """
    전체/in-sample(앞 in_sample_frac)/out-of-sample(뒤 나머지) 세 가지 결과를 낸다.

    holding은 반드시 종가 전체 기간으로 계산한 신호를 그대로 넘겨야 한다
    (분할한 조각으로 다시 계산하면 안 됨) — 지표가 이동평균처럼 과거 n일을
    보는데, out-of-sample 조각만 떼어 다시 계산하면 그 구간 앞쪽에 불필요한
    NaN 구간이 생긴다. 분할은 '거래를 진입한 날짜'가 어느 구간에 속하는지로만
    나눈다.
    """
    close = close.sort_index()
    ret, eff, trades = _simulate(close, holding, open_, commission_rate, tax_rate)

    n = len(close)
    split_pos = int(n * in_sample_frac)
    in_idx = close.index[:split_pos]
    out_idx = close.index[split_pos:]

    return {
        "full": result_for_window(ret, eff, trades, f"{label} (전체)".strip()),
        "in_sample": result_for_window(ret.loc[in_idx], eff.loc[in_idx], trades, f"{label} (in-sample)".strip()),
        "out_of_sample": result_for_window(ret.loc[out_idx], eff.loc[out_idx], trades, f"{label} (out-of-sample)".strip()),
    }
