"""analysis/price_limits.py 테스트 — 합성 데이터만 사용(DB 비의존)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from analysis import price_limits as pl


class TestLimitPctOn:
    def test_known_regime_returns_30(self):
        assert pl.limit_pct_on(date(2020, 1, 1)) == 30.0

    def test_exactly_on_effective_date_included(self):
        assert pl.limit_pct_on(date(2015, 6, 15)) == 30.0

    def test_before_known_schedule_returns_none(self):
        assert pl.limit_pct_on(date(2010, 1, 1)) is None

    def test_custom_schedule_overrides_default(self):
        custom = [pl.PriceLimitChange(effective_on=date(2000, 1, 1), limit_pct=15.0, source_note="test")]
        assert pl.limit_pct_on(date(2020, 1, 1), schedule=custom) == 15.0
        assert pl.limit_pct_on(date(1999, 1, 1), schedule=custom) is None


class TestFindExceedances:
    def test_flags_return_beyond_limit_in_known_regime(self):
        idx = pd.bdate_range("2020-01-01", periods=3)
        closes = pd.Series([100.0, 140.0, 141.0], index=idx)  # day1: +40% > 30%+0.5 허용오차
        out = pl.find_exceedances({"T1": closes})
        assert len(out) == 1
        assert out[0].ticker == "T1"
        assert out[0].return_pct == pytest.approx(40.0)
        assert out[0].limit_pct == 30.0

    def test_within_limit_not_flagged(self):
        idx = pd.bdate_range("2020-01-01", periods=2)
        closes = pd.Series([100.0, 125.0], index=idx)  # +25% < 30%
        assert pl.find_exceedances({"T1": closes}) == []

    def test_tolerance_allows_small_overage(self):
        idx = pd.bdate_range("2020-01-01", periods=2)
        closes = pd.Series([100.0, 130.3], index=idx)  # +30.3%, 허용오차 0.5%p 안쪽
        assert pl.find_exceedances({"T1": closes}, tolerance_pct=0.5) == []

    def test_unknown_regime_dates_are_skipped_not_flagged(self):
        idx = pd.bdate_range("2010-01-01", periods=2)  # 스케줄 이전 날짜
        closes = pd.Series([100.0, 500.0], index=idx)  # 극단적 변동이어도 기준을 모르면 판정 안 함
        assert pl.find_exceedances({"T1": closes}) == []

    def test_multiple_tickers_scanned_independently(self):
        idx = pd.bdate_range("2020-01-01", periods=2)
        data = {
            "A": pd.Series([100.0, 140.0], index=idx),
            "B": pd.Series([100.0, 110.0], index=idx),
        }
        out = pl.find_exceedances(data)
        assert {e.ticker for e in out} == {"A"}

    def test_negative_exceedance_also_flagged(self):
        idx = pd.bdate_range("2020-01-01", periods=2)
        closes = pd.Series([100.0, 60.0], index=idx)  # -40%
        out = pl.find_exceedances({"T1": closes})
        assert len(out) == 1
        assert out[0].return_pct == pytest.approx(-40.0)
