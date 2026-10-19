"""
2단계(거래비용 파라미터화): slippage_rate·tax_rate_schedule이
analysis/pullback.py·analysis/price_rule_backtest.py에 실제로 반영되는지
손으로 계산 가능한 작은 거래로 회귀 테스트.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from analysis import price_rule_backtest as prb
from analysis import pullback as pb
from analysis import trading_costs as tc


def _simple_pullback_ohlc():
    idx = pd.bdate_range("2026-01-01", periods=3)
    return pd.DataFrame({
        "open": [100, 100, 99],
        "high": [100, 101, 101],
        "low": [100, 97, 97.5],
        "close": [100, 99, 100],
    }, index=idx)


class TestPullbackSlippage:
    def test_slippage_reduces_return_symmetrically(self):
        ohlc = _simple_pullback_ohlc()
        no_slip = pb.simulate_pullback_trades(ohlc, commission_rate=0, tax_rate=0, slippage_rate=0.0)
        with_slip = pb.simulate_pullback_trades(ohlc, commission_rate=0, tax_rate=0, slippage_rate=0.01)
        assert len(no_slip) == 1 and len(with_slip) == 1
        # entry=98(지정가), exit=target(98*1.02=99.96) — 손으로 계산
        entry, target = 98.0, 98.0 * 1.02
        expected_no_slip = (target / entry - 1) * 100
        expected_with_slip = ((target * (1 - 0.01)) / (entry * (1 + 0.01)) - 1) * 100
        assert no_slip[0].return_pct == pytest.approx(expected_no_slip)
        assert with_slip[0].return_pct == pytest.approx(expected_with_slip)
        assert with_slip[0].return_pct < no_slip[0].return_pct


class TestPullbackTaxSchedule:
    def test_schedule_overrides_flat_tax_rate_by_exit_date(self):
        ohlc = _simple_pullback_ohlc()
        exit_date = ohlc.index[2].date()  # 거래가 이 날 청산됨(손으로 추적)
        schedule = [tc.RateChange(exit_date, 0.05, "test")]  # 극단적으로 큰 세율로 효과를 뚜렷하게
        with_flat = pb.simulate_pullback_trades(ohlc, commission_rate=0, tax_rate=0.0)
        with_schedule = pb.simulate_pullback_trades(ohlc, commission_rate=0, tax_rate=0.0, tax_rate_schedule=schedule)
        assert with_schedule[0].exit_date.date() == exit_date
        assert with_schedule[0].return_pct < with_flat[0].return_pct

    def test_schedule_none_falls_back_to_flat_rate(self):
        ohlc = _simple_pullback_ohlc()
        a = pb.simulate_pullback_trades(ohlc, commission_rate=0, tax_rate=0.002, tax_rate_schedule=None)
        b = pb.simulate_pullback_trades(ohlc, commission_rate=0, tax_rate=0.002)
        assert a[0].return_pct == pytest.approx(b[0].return_pct)


def _simple_price_rule_setup():
    idx = pd.bdate_range("2026-01-01", periods=3)
    ohlc = pd.DataFrame({
        "open": [100, 100, 99], "high": [100, 101, 101],
        "low": [100, 97, 97.5], "close": [100, 99, 100],
    }, index=idx)
    levels = pd.DataFrame({
        "entry_price": [98, None, None],
        "target_price": [103, None, None],
        "stop_price": [95, None, None],
    }, index=idx)
    return ohlc, levels


class TestPriceRuleBacktestCostWiring:
    def test_slippage_applies(self):
        # target(103)·stop(95) 둘 다 안 닿게 두고 max_hold_days=1로 강제
        # 종가 청산시켜서(day2 종가=100) 체결가를 정확히 손으로 계산한다.
        ohlc, levels = _simple_price_rule_setup()
        no_slip = prb.simulate_price_rule_trades(
            ohlc, levels, entry_type="limit", max_hold_days=1, commission_rate=0, tax_rate=0,
        )
        with_slip = prb.simulate_price_rule_trades(
            ohlc, levels, entry_type="limit", max_hold_days=1, commission_rate=0, tax_rate=0, slippage_rate=0.02,
        )
        assert len(no_slip) == 1 and len(with_slip) == 1
        assert with_slip[0].return_pct < no_slip[0].return_pct

    def test_tax_schedule_applies_on_exit_date(self):
        ohlc, levels = _simple_price_rule_setup()
        exit_date = ohlc.index[2].date()
        schedule = [tc.RateChange(exit_date, 0.05, "test")]
        flat = prb.simulate_price_rule_trades(
            ohlc, levels, entry_type="limit", max_hold_days=1, commission_rate=0, tax_rate=0.0,
        )
        scheduled = prb.simulate_price_rule_trades(
            ohlc, levels, entry_type="limit", max_hold_days=1, commission_rate=0, tax_rate=0.0,
            tax_rate_schedule=schedule,
        )
        assert len(flat) == 1 and len(scheduled) == 1
        assert scheduled[0].exit_date.date() == exit_date
        assert scheduled[0].return_pct < flat[0].return_pct
