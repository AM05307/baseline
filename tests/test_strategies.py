"""
기술적 지표/전략/규칙 백테스트 테스트.

실제 yfinance 호출 없이 전부 합성 데이터로 검증한다. DB도 건드리지 않는다
(analysis/* 모듈은 순수 함수라 애초에 DB/API를 모른다).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis import indicators as ind
from analysis import rule_backtest as rb
from analysis import strategies as strat


def _series(vals, start="2026-01-01") -> pd.Series:
    idx = pd.bdate_range(start, periods=len(vals))
    return pd.Series(vals, index=idx, dtype=float)


def _random_walk(n: int, seed: int = 0, start: str = "2026-01-01") -> pd.Series:
    rs = np.random.RandomState(seed)
    vals = np.cumprod(1 + rs.normal(0, 0.01, n)) * 100
    return _series(vals, start)


class TestIndicators:
    def test_sma_known_values(self):
        s = _series([1, 2, 3, 4, 5])
        out = ind.sma(s, 3)
        assert out.iloc[:2].isna().all()
        assert out.iloc[2] == pytest.approx(2.0)
        assert out.iloc[3] == pytest.approx(3.0)
        assert out.iloc[4] == pytest.approx(4.0)

    def test_rsi_is_100_when_no_losses(self):
        s = _series(list(range(1, 40)))  # 계속 오르기만 함 -> 손실 없음
        r = ind.rsi(s, n=14)
        assert r.iloc[-1] == pytest.approx(100.0)

    def test_rsi_bounded_0_100(self):
        s = _random_walk(120)
        r = ind.rsi(s, n=14).dropna()
        assert (r >= 0).all() and (r <= 100).all()

    def test_bollinger_flat_price_has_zero_width(self):
        s = _series([100.0] * 25)
        middle, upper, lower = ind.bollinger(s, n=20, k=2)
        assert middle.iloc[-1] == pytest.approx(100.0)
        assert upper.iloc[-1] == pytest.approx(100.0)
        assert lower.iloc[-1] == pytest.approx(100.0)

    def test_macd_zero_on_flat_price(self):
        s = _series([100.0] * 60)
        macd_line, signal_line = ind.macd(s)
        assert macd_line.iloc[-1] == pytest.approx(0.0, abs=1e-9)
        assert signal_line.iloc[-1] == pytest.approx(0.0, abs=1e-9)


class TestNoLookahead:
    """지표/전략이 미래 데이터를 참조하지 않는지: 마지막 날 값을 바꿔도
    그 이전 날짜의 결과가 안 바뀌어야 한다."""

    @pytest.mark.parametrize(
        "fn",
        [
            lambda s: ind.sma(s, 10),
            lambda s: ind.rsi(s, 14),
            lambda s: ind.bollinger(s, 20, 2)[0],
            lambda s: ind.macd(s)[0],
            strat.ma_cross,
            strat.rsi_reversion,
            strat.bollinger_reversion,
            strat.macd_cross,
            lambda s: strat.ensemble(s, threshold=0.5),
        ],
    )
    def test_changing_last_day_does_not_change_earlier_output(self, fn):
        s = _random_walk(60)
        s2 = s.copy()
        s2.iloc[-1] = s2.iloc[-1] * 1.5
        before = fn(s)
        after = fn(s2)
        assert before.iloc[:-1].equals(after.iloc[:-1])


class TestEnsemble:
    def test_threshold_controls_how_many_votes_needed(self, monkeypatch):
        idx = pd.bdate_range("2026-01-01", periods=3)
        two_of_four = [
            pd.Series([1, 1, 0], index=idx),
            pd.Series([1, 0, 0], index=idx),
            pd.Series([0, 1, 1], index=idx),
            pd.Series([0, 0, 1], index=idx),
        ]
        monkeypatch.setattr(strat, "ma_cross", lambda s, **kw: two_of_four[0])
        monkeypatch.setattr(strat, "rsi_reversion", lambda s, **kw: two_of_four[1])
        monkeypatch.setattr(strat, "bollinger_reversion", lambda s, **kw: two_of_four[2])
        monkeypatch.setattr(strat, "macd_cross", lambda s, **kw: two_of_four[3])

        dummy = pd.Series([1.0, 2.0, 3.0], index=idx)
        # 매 날짜 정확히 2/4 투표 -> score=0.5. threshold=0.5면 보유, 0.75면 현금.
        at_half = strat.ensemble(dummy, threshold=0.5)
        at_three_quarter = strat.ensemble(dummy, threshold=0.75)
        assert (at_half == 1).all()
        assert (at_three_quarter == 0).all()


class TestRuleBacktestExecutionTiming:
    """핵심 검증: t일 종가 신호 -> t+1일 시가 체결. 결정일의 종가가 아니라
    그 다음날 시가로 체결가가 정해지는지 숫자로 확인한다."""

    def _setup(self):
        dates = pd.bdate_range("2026-01-01", periods=10)
        close = pd.Series([100, 101, 102, 103, 104, 105, 106, 107, 108, 109], index=dates, dtype=float)
        open_ = close - 0.5  # 시가는 종가와 다른 값으로 둬서 혼동 여부를 드러냄
        # eff(실제 보유) = holding.shift(1): 0,0,0,1,1,1,0,0,1,1
        holding = pd.Series([0, 0, 1, 1, 1, 0, 0, 1, 1, 1], index=dates)
        return dates, close, open_, holding

    def test_entry_and_exit_use_next_day_open_not_decision_day_close(self):
        dates, close, open_, holding = self._setup()
        result = rb.backtest_rule(close, holding, open_, commission_rate=0, tax_rate=0)["full"]

        assert len(result.trades) == 1  # 마지막 포지션은 기간 끝까지 안 닫혀서 미포함
        expected_entry = open_.loc[dates[3]]
        expected_exit = open_.loc[dates[6]]
        expected_return = (expected_exit / expected_entry - 1) * 100
        assert result.trades[0] == pytest.approx(expected_return)

        # 결정일(인덱스 2)의 종가가 체결가로 쓰였다면 다른 값이 나왔을 것
        wrong_using_decision_close = (open_.loc[dates[6]] / close.loc[dates[2]] - 1) * 100
        assert result.trades[0] != pytest.approx(wrong_using_decision_close)

    def test_missing_open_falls_back_to_close(self):
        dates, close, open_, holding = self._setup()
        open_ = open_.copy()
        open_.loc[dates[3]] = float("nan")  # 진입일 시가 없음 -> 그날 종가로 체결
        result = rb.backtest_rule(close, holding, open_, commission_rate=0, tax_rate=0)["full"]
        expected_entry = close.loc[dates[3]]
        expected_exit = open_.loc[dates[6]]
        expected_return = (expected_exit / expected_entry - 1) * 100
        assert result.trades[0] == pytest.approx(expected_return)

    def test_later_price_changes_do_not_affect_already_closed_trade(self):
        dates, close, open_, holding = self._setup()
        result1 = rb.backtest_rule(close, holding, open_, commission_rate=0, tax_rate=0)["full"]

        close2, open2 = close.copy(), open_.copy()
        close2.iloc[-1] *= 2
        open2.iloc[-1] *= 2
        result2 = rb.backtest_rule(close2, holding, open2, commission_rate=0, tax_rate=0)["full"]

        assert result1.trades == result2.trades


class TestTransactionCosts:
    def test_higher_costs_reduce_trade_return(self):
        dates = pd.bdate_range("2026-01-01", periods=6)
        close = pd.Series([100, 102, 104, 106, 108, 110], index=dates, dtype=float)
        open_ = close.copy()
        holding = pd.Series([0, 1, 1, 0, 0, 0], index=dates)  # 한 번의 라운드트립

        free = rb.backtest_rule(close, holding, open_, commission_rate=0, tax_rate=0)["full"]
        costly = rb.backtest_rule(close, holding, open_, commission_rate=0.01, tax_rate=0.01)["full"]

        assert len(free.trades) == 1 and len(costly.trades) == 1
        assert costly.trades[0] < free.trades[0]


class TestReliability:
    def test_fewer_than_30_trades_is_unreliable(self):
        r = rb.RuleBacktestResult(label="t", trades=[1.0, -2.0, 3.0])
        assert r.n == 3
        assert not r.reliable
        assert "부족" in r.report()

    def test_30_or_more_trades_is_reliable(self):
        r = rb.RuleBacktestResult(label="t", trades=[1.0] * 30)
        assert r.reliable

    def test_backtest_rule_flags_unreliable_with_few_trades(self):
        s = _random_walk(80)
        holding = strat.ma_cross(s)
        result = rb.backtest_rule(s, holding, None)["full"]
        assert result.n < rb.MIN_SAMPLES
        assert not result.reliable
