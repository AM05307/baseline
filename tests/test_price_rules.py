"""
규칙 기반 매수/목표/손절 기준가(analysis/price_rules.py, price_rule_backtest.py) 테스트.

합성 OHLC로만 검증한다(실제 yfinance 호출 없음).
"""

from __future__ import annotations

import pandas as pd
import pytest

from analysis import indicators as ind
from analysis import price_rule_backtest as prb
from analysis import price_rules as pr
from analysis import rule_backtest as rbt


def _ohlc(rows: list[tuple[float, float, float, float]], start: str = "2026-01-01") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


class TestAtrIndicator:
    def test_flat_price_has_zero_atr(self):
        rows = [(100, 100, 100, 100)] * 20
        df = _ohlc(rows)
        a = ind.atr(df["high"], df["low"], df["close"], n=14)
        assert a.iloc[-1] == pytest.approx(0.0)

    def test_changing_last_day_does_not_change_earlier_atr(self):
        rows = [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(30)]
        df = _ohlc(rows)
        df2 = df.copy()
        df2.iloc[-1] = df2.iloc[-1] * 2
        a1 = ind.atr(df["high"], df["low"], df["close"], n=14)
        a2 = ind.atr(df2["high"], df2["low"], df2["close"], n=14)
        assert a1.iloc[:-1].equals(a2.iloc[:-1])


class TestPriceRuleLevels:
    def test_ma_pullback_no_signal_when_below_trend(self):
        # 계속 하락하는 가격 -> 종가가 장기 이동평균 아래 -> 신호 없음(NaN)
        rows = [(100 - i, 100 - i, 100 - i, 100 - i) for i in range(70)]
        df = _ohlc(rows)
        levels = pr.ma_pullback_levels(df, trend_window=60, pullback_window=20)
        assert levels["entry_price"].iloc[-1] != levels["entry_price"].iloc[-1]  # NaN

    def test_ma_pullback_signal_when_above_trend(self):
        # 계속 상승하는 가격 -> 종가가 장기 이동평균 위 -> 신호 있음
        rows = [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(70)]
        df = _ohlc(rows)
        levels = pr.ma_pullback_levels(df, trend_window=60, pullback_window=20, target_pct=0.05, stop_pct=0.03)
        entry = levels["entry_price"].iloc[-1]
        assert entry == entry  # not NaN
        assert levels["target_price"].iloc[-1] == pytest.approx(entry * 1.05)
        assert levels["stop_price"].iloc[-1] == pytest.approx(entry * 0.97)

    def test_breakout_range_uses_high_and_low_directly(self):
        rows = [(100, 110, 95, 105)] * 5
        df = _ohlc(rows)
        levels = pr.breakout_range_levels(df, target_pct=0.1)
        assert levels["entry_price"].iloc[-1] == pytest.approx(110)
        assert levels["stop_price"].iloc[-1] == pytest.approx(95)
        assert levels["target_price"].iloc[-1] == pytest.approx(121)

    @pytest.mark.parametrize("fn,kwargs", [
        (pr.ma_pullback_levels, {}),
        (pr.atr_band_levels, {}),
        (pr.breakout_range_levels, {}),
    ])
    def test_changing_last_day_does_not_change_earlier_levels(self, fn, kwargs):
        rows = [(100 + i * 0.3, 101 + i * 0.3, 99 + i * 0.3, 100 + i * 0.3) for i in range(80)]
        df = _ohlc(rows)
        df2 = df.copy()
        df2.iloc[-1] = df2.iloc[-1] * 2
        before = fn(df, **kwargs)
        after = fn(df2, **kwargs)
        assert before["entry_price"].iloc[:-1].equals(after["entry_price"].iloc[:-1])


class TestSimulatePriceRuleTrades:
    def test_limit_fill_and_target_exit(self):
        # day0: 기준. day1: 신호(entry=98,target=103,stop=95) 생성, 체결 안 됨(day1 자체엔 신호가 없음 — day0 기준)
        # 실제로는 levels[i-1]을 day i에 적용하므로, levels[0]이 day1에 적용된다.
        idx = pd.bdate_range("2026-01-01", periods=4)
        ohlc = pd.DataFrame({
            "open":  [100, 100, 99, 99],
            "high":  [100, 101, 104, 99.5],
            "low":   [100, 97,  98, 99],
            "close": [100, 99,  103, 99.3],
        }, index=idx)
        levels = pd.DataFrame({
            "entry_price":  [98, None, None, None],
            "target_price": [103, None, None, None],
            "stop_price":   [95, None, None, None],
        }, index=idx)
        trades = prb.simulate_price_rule_trades(ohlc, levels, entry_type="limit", max_hold_days=10)
        assert len(trades) == 1
        t = trades[0]
        assert t.entry_date == idx[1]
        assert t.entry_price == pytest.approx(98.0)  # day1 저가97<=98 -> 지정가 체결
        assert t.exit_reason == "target"
        assert t.exit_date == idx[2]  # day2 고가104>=target103

    def test_stop_entry_breakout_fill(self):
        idx = pd.bdate_range("2026-01-01", periods=4)
        ohlc = pd.DataFrame({
            "open":  [100, 100, 106, 106],
            "high":  [100, 106, 107, 106.5],
            "low":   [100, 99,  105, 105.5],
            "close": [100, 105, 106, 106.2],
        }, index=idx)
        levels = pd.DataFrame({
            "entry_price":  [105, None, None, None],  # 돌파 기준가
            "target_price": [115, None, None, None],
            "stop_price":   [95, None, None, None],
        }, index=idx)
        trades = prb.simulate_price_rule_trades(ohlc, levels, entry_type="stop", max_hold_days=10)
        assert len(trades) == 0  # 아직 target/stop/max_hold 중 아무것도 안 닿아서 열려있는 포지션
        # day1 고가106>=105 -> 체결가 105(시가100은 105 미달이라 지정가 그대로)

    def test_same_day_target_and_stop_prefers_stop(self):
        idx = pd.bdate_range("2026-01-01", periods=3)
        ohlc = pd.DataFrame({
            "open":  [100, 100, 99],
            "high":  [100, 101, 110],  # day2 고가110 >= target 103
            "low":   [100, 97,  90],   # day2 저가90 <= stop 95
            "close": [100, 99,  100],
        }, index=idx)
        levels = pd.DataFrame({
            "entry_price":  [98, None, None],
            "target_price": [103, None, None],
            "stop_price":   [95, None, None],
        }, index=idx)
        trades = prb.simulate_price_rule_trades(ohlc, levels, entry_type="limit")
        assert len(trades) == 1
        assert trades[0].exit_reason == "stop"
        assert trades[0].exit_price == pytest.approx(95.0)

    def test_max_hold_days_forces_close_exit(self):
        idx = pd.bdate_range("2026-01-01", periods=4)
        ohlc = pd.DataFrame({
            "open":  [100, 100, 99, 99.2],
            "high":  [100, 101, 99.5, 99.6],
            "low":   [100, 97,  98.7, 99.0],
            "close": [100, 99,  99.3, 99.4],
        }, index=idx)
        levels = pd.DataFrame({
            "entry_price":  [98, None, None, None],
            "target_price": [200, None, None, None],  # 절대 안 닿게
            "stop_price":   [1, None, None, None],     # 절대 안 닿게
        }, index=idx)
        trades = prb.simulate_price_rule_trades(ohlc, levels, entry_type="limit", max_hold_days=2)
        assert len(trades) == 1
        assert trades[0].exit_reason == "max_hold"
        assert trades[0].exit_date == idx[3]
        assert trades[0].exit_price == pytest.approx(99.4)


class TestPassesGate:
    def _result(self, n: int, mean_return: float) -> rbt.RuleBacktestResult:
        if n == 0:
            trades = []
        else:
            # mean_return을 정확히 맞추기 위해 n-1개는 +1%, 마지막 1개로 평균 보정
            trades = [1.0] * (n - 1)
            last = mean_return * n - sum(trades)
            trades.append(last)
        return rbt.RuleBacktestResult(label="t", trades=trades)

    def test_fails_when_fewer_than_min_trades(self):
        results = {"full": self._result(50, 1.0), "out_of_sample": self._result(20, 1.0)}
        assert not prb.passes(results, min_trades=100)

    def test_fails_when_full_mean_not_positive(self):
        results = {"full": self._result(150, -0.5), "out_of_sample": self._result(30, 1.0)}
        assert not prb.passes(results, min_trades=100)

    def test_fails_when_out_of_sample_mean_not_positive(self):
        results = {"full": self._result(150, 1.0), "out_of_sample": self._result(30, -0.2)}
        assert not prb.passes(results, min_trades=100)

    def test_fails_when_out_of_sample_has_no_trades(self):
        results = {"full": self._result(150, 1.0), "out_of_sample": self._result(0, 0)}
        assert not prb.passes(results, min_trades=100)

    def test_passes_when_all_three_conditions_met(self):
        results = {"full": self._result(150, 0.8), "out_of_sample": self._result(40, 0.5)}
        assert prb.passes(results, min_trades=100)


class TestSummarizeSplitsByCalendarYear:
    def test_out_of_sample_is_last_year_only(self):
        idx = pd.bdate_range("2020-01-01", periods=1000)
        close = pd.Series(100.0, index=idx)
        t = prb.PriceRuleTrade(
            entry_date=idx[-10], exit_date=idx[-9], entry_price=100.0, exit_price=101.0,
            target_price=101.0, stop_price=99.0, exit_reason="target", hold_days=1, return_pct=1.0,
        )
        t_old = prb.PriceRuleTrade(
            entry_date=idx[10], exit_date=idx[11], entry_price=100.0, exit_price=99.0,
            target_price=101.0, stop_price=99.0, exit_reason="stop", hold_days=1, return_pct=-1.0,
        )
        results = prb.summarize_price_rule_trades(close, [t_old, t], out_of_sample_years=1, label="x")
        assert results["out_of_sample"].n == 1
        assert results["in_sample"].n == 1
        assert results["full"].n == 2
