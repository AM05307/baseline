"""analysis/adr.py 테스트 (기존에 테스트가 없던 모듈 — 6단계 작업 중 확인됨)."""

from __future__ import annotations

import pandas as pd
import pytest

from analysis import adr


class TestParityValue:
    def test_basic_conversion(self):
        # KRX 184,100원, 환율 1,380원/달러, 전환비율 10 -> 패리티 = 184100/1380/10
        v = adr.parity_value(184100, 1380, ratio=10)
        assert v == pytest.approx(184100 / 1380 / 10)

    def test_invalid_rate_or_ratio_returns_nan(self):
        assert adr.parity_value(100, 0, ratio=10) != adr.parity_value(100, 0, ratio=10)  # NaN
        assert adr.parity_value(100, 1000, ratio=0) != adr.parity_value(100, 1000, ratio=0)


class TestPremium:
    def test_positive_premium_when_adr_trades_above_parity(self):
        p = adr.premium(krx_close_krw=184100, adr_close_usd=20.0, usd_krw_rate=1380, ratio=10)
        assert p.premium_pct > 0
        assert p.ratio == 10

    def test_custom_ratio_changes_parity_and_premium(self):
        p10 = adr.premium(krx_close_krw=184100, adr_close_usd=15.0, usd_krw_rate=1380, ratio=10)
        p5 = adr.premium(krx_close_krw=184100, adr_close_usd=15.0, usd_krw_rate=1380, ratio=5)
        assert p10.ratio == 10 and p5.ratio == 5
        assert p10.parity_usd != pytest.approx(p5.parity_usd)

    def test_default_ratio_used_when_not_specified(self):
        p = adr.premium(krx_close_krw=184100, adr_close_usd=15.0, usd_krw_rate=1380)
        assert p.ratio == adr.DEFAULT_RATIO


class TestPremiumSeries:
    def test_aligns_on_common_dates_and_drops_na(self):
        idx = pd.bdate_range("2026-01-01", periods=5)
        krx = pd.Series([184100.0] * 5, index=idx)
        fx = pd.Series([1380.0] * 5, index=idx)
        adr_close = pd.Series([15.0, 16.0, float("nan"), 17.0, 18.0], index=idx)
        out = adr.premium_series(krx, adr_close, fx, ratio=10)
        assert len(out) == 4  # NaN 행 제외

    def test_empty_when_no_common_dates(self):
        a = pd.Series([1.0], index=pd.bdate_range("2026-01-01", periods=1))
        b = pd.Series([1.0], index=pd.bdate_range("2026-06-01", periods=1))
        c = pd.Series([1.0], index=pd.bdate_range("2026-01-01", periods=1))
        out = adr.premium_series(a, b, c, ratio=10)
        assert out.empty
