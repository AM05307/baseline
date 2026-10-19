"""
단순 이벤트 백테스트.

설계 원칙: 승률만 크게 보여주지 않는다.
표본 수, 최대 손실, 표준편차를 항상 함께 반환한다.
n이 작으면 경고를 붙인다. 과적합을 화면에서 막는 게 목적이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

MIN_SAMPLES = 30


@dataclass
class BacktestResult:
    label: str
    trades: list[float] = field(default_factory=list)  # 각 회차 수익률(%)

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
        return sum(self.trades) / self.n if self.trades else float("nan")

    @property
    def worst(self) -> float:
        return min(self.trades) if self.trades else float("nan")

    @property
    def best(self) -> float:
        return max(self.trades) if self.trades else float("nan")

    @property
    def std(self) -> float:
        s = pd.Series(self.trades)
        return float(s.std()) if self.n > 1 else float("nan")

    @property
    def reliable(self) -> bool:
        return self.n >= MIN_SAMPLES

    def report(self) -> str:
        """
        승률 위에 최대손실과 표본수를 같은 비중으로 놓는다.
        보기 좋게 만들지 않는 게 이 함수의 목적이다.
        """
        lines = [
            f"백테스트: {self.label}",
            "─" * 42,
            f"표본        {self.n}회",
            f"승률        {self.win_rate:.1f}%",
            f"평균수익    {self.mean_return:+.2f}%",
            f"표준편차    {self.std:.2f}%p",
            f"최고        {self.best:+.2f}%",
            f"최대손실    {self.worst:+.2f}%",
        ]
        if not self.reliable:
            lines += [
                "",
                f"⚠ 표본 {self.n}개는 통계적 결론을 내기에 부족합니다.",
                f"  (권장 {MIN_SAMPLES}회 이상). 우연일 가능성을 배제할 수 없습니다.",
            ]
        return "\n".join(lines)


def event_window_returns(
    prices: pd.Series,
    event_dates: list[date],
    entry_offset: int = -10,
    exit_offset: int = -1,
    label: str = "이벤트 전 구간",
) -> BacktestResult:
    """
    이벤트 기준 상대일로 진입/청산했을 때의 수익률.

    entry_offset=-10, exit_offset=-1 이면
    '이벤트 10거래일 전 종가 매수 → 1거래일 전 종가 매도'.

    거래비용(수수료+세금 약 0.2%)은 반영하지 않는다.
    실제 결과는 이보다 낮다.
    """
    s = prices.sort_index()
    idx = s.index
    res = BacktestResult(label=label)

    for ev in event_dates:
        ts = pd.Timestamp(ev)
        pos = idx.searchsorted(ts)
        i_in, i_out = pos + entry_offset, pos + exit_offset
        if i_in < 0 or i_out >= len(s) or i_in >= i_out:
            continue
        entry, exit_ = s.iloc[i_in], s.iloc[i_out]
        if entry:
            res.trades.append((exit_ / entry - 1) * 100)

    return res
