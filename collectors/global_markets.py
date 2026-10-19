"""
해외 보조 지표 수집기 — 전문가 경험칙 신호(analysis/signals.py)가 쓰는
"주변부 시장" 데이터. yfinance, API 키 불필요.

티커 선택 메모:
    ^KQ11      코스닥
    ^GSPC      S&P500
    ^DJI       다우
    ^STOXX50E  Euro Stoxx 50
    ^BSESN     인도 Sensex
    ^BVSP      브라질 Bovespa
    VNM        베트남 — yfinance에 VN지수(^VNINDEX) 자체가 없어서 VanEck
               Vietnam ETF(VNM)를 대리 지표로 쓴다. 지수와 완전히 같지
               않다(ETF라 운용비용·추적오차 있음) — 확인 필요.
    XU100.IS   튀르키예 BIST100
    SI=F       은 선물(전문가 A가 언급한 '은'을 선물로 해석)
    IPO        IPO ETF (Renaissance IPO ETF) — KHJ_IPO_WEAK가 씀
    ^TNX       미국 10년물 국채 수익률. 종가가 이미 %값 그대로(예: 4.5 = 4.5%),
               10배 안 해도 된다 — 수집 시점에 직접 확인함.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf  # noqa: E402

from db import store  # noqa: E402

TICKERS = {
    "^KQ11": "KOSDAQ",
    "^GSPC": "SPX",
    "^DJI": "DJI",
    "^STOXX50E": "STOXX50",
    "^BSESN": "SENSEX",
    "^BVSP": "BOVESPA",
    "VNM": "VNINDEX",
    "XU100.IS": "BIST100",
    "SI=F": "SILVER",
    "IPO": "IPOETF",
    "^TNX": "US10Y",
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
            print(f"  {symbol:10s} {n:5d} rows")
        except Exception as e:  # 한 종목 실패가 전체를 막지 않게
            print(f"  {symbol:10s} FAILED: {e}")
            store.log_run("global_markets", "fail", 0, f"{symbol}: {e}")
    store.log_run("global_markets", "ok", total)
    return total


if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "1y"
    print(f"[global_markets] period={period}")
    print(f"완료: {collect_all(period)} rows")
