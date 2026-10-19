"""yfinance가 배당일 등에 끼워 넣는 가격 없는(NaN) 행은 저장하지 않는다 — 069500 수집에서 실제로 5행 발견(2026-10-19)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors import global_markets, kr_market, us_market  # noqa: E402


class _FakeTicker:
    def __init__(self, *_a, **_k):
        pass

    def history(self, *a, **k):
        nan = float("nan")
        return pd.DataFrame({"Open": [100.0, nan, 102.0], "High": [101.0, nan, 103.0], "Low": [99.0, nan, 101.0],
                             "Close": [100.5, nan, 102.5], "Volume": [10, 0, 12]},
                            index=pd.DatetimeIndex(["2026-10-14", "2026-10-15", "2026-10-16"]))


@pytest.mark.parametrize("mod", [kr_market, us_market, global_markets])
def test_nan_price_rows_are_skipped(mod, monkeypatch):
    monkeypatch.setattr(mod.yf, "Ticker", _FakeTicker)
    sym = next(iter(mod.TICKERS))
    rows = mod.fetch(sym, "max")
    assert [r["date"] for r in rows] == ["2026-10-14", "2026-10-16"]
    assert all(r["close"] == r["close"] for r in rows)
