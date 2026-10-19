"""
미국 시장 수집기.

yfinance를 쓴다. API 키가 필요 없어서 지금 바로 돌아간다.
한투 API 발급 전까지 이걸로 파이프라인 전체를 검증할 수 있다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf  # noqa: E402

from db import store  # noqa: E402

# ^SOX = 필라델피아 반도체지수, ^VIX = CBOE 변동성지수 (analysis/sentiment.py가 씀)
TICKERS = {
    "^SOX": "SOX",
    "NVDA": "NVDA",
    "MU": "MU",
    "TSM": "TSM",
    "AMD": "AMD",
    "^VIX": "VIX",
    "SPY": "SPY",  # 국장/미장 비교 백테스트(scripts/market_compare.py) — 2026-10-19 추가
    "QQQ": "QQQ",
}


def fetch(symbol: str, period: str = "1y") -> list[dict]:
    df = yf.Ticker(symbol).history(period=period, auto_adjust=False)
    if df.empty:
        return []
    name = TICKERS.get(symbol, symbol)
    return [
        {
            "ticker": name,
            "date": idx.strftime("%Y-%m-%d"),
            "open": float(r.Open), "high": float(r.High),
            "low": float(r.Low), "close": float(r.Close),
            "volume": int(r.Volume) if r.Volume == r.Volume else 0,
            "source": "yfinance",
        }
        for idx, r in df.iterrows() if r.Close == r.Close  # 가격 없는 행(배당일 등 yfinance가 끼워 넣는 NaN 행)은 버린다
    ]


def collect_all(period: str = "1y") -> int:
    store.init_db()
    total = 0
    for symbol in TICKERS:
        try:
            rows = fetch(symbol, period)
            n = store.upsert_prices(rows)
            total += n
            print(f"  {symbol:6s} {n:5d} rows")
        except Exception as e:  # 한 종목 실패가 전체를 막지 않게
            print(f"  {symbol:6s} FAILED: {e}")
            store.log_run("us_market", "fail", 0, f"{symbol}: {e}")
    store.log_run("us_market", "ok", total)
    return total


if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "1y"
    print(f"[us_market] period={period}")
    print(f"완료: {collect_all(period)} rows")
