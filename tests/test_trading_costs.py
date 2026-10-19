"""analysis/trading_costs.py 테스트 — 날짜별 세율 조회."""

from __future__ import annotations

from datetime import date

from analysis import trading_costs as tc


class TestTaxRateOn:
    def test_uses_default_schedule_when_none_given(self):
        rate = tc.tax_rate_on(date(2026, 1, 1))
        assert rate == tc.TAX_RATE_SCHEDULE[0].tax_rate

    def test_picks_most_recent_applicable_entry(self):
        schedule = [
            tc.RateChange(date(2020, 1, 1), 0.0023, "a"),
            tc.RateChange(date(2023, 1, 1), 0.0020, "b"),
            tc.RateChange(date(2025, 1, 1), 0.0015, "c"),
        ]
        assert tc.tax_rate_on(date(2021, 6, 1), schedule) == 0.0023
        assert tc.tax_rate_on(date(2023, 6, 1), schedule) == 0.0020
        assert tc.tax_rate_on(date(2026, 1, 1), schedule) == 0.0015

    def test_exact_effective_date_is_inclusive(self):
        schedule = [tc.RateChange(date(2023, 1, 1), 0.0020, "a")]
        assert tc.tax_rate_on(date(2023, 1, 1), schedule) == 0.0020

    def test_date_before_earliest_entry_extrapolates_to_earliest(self):
        schedule = [tc.RateChange(date(2023, 1, 1), 0.0020, "a")]
        assert tc.tax_rate_on(date(2000, 1, 1), schedule) == 0.0020

    def test_empty_schedule_returns_zero(self):
        assert tc.tax_rate_on(date(2026, 1, 1), []) == 0.0
