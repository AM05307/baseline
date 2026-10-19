"""
하이닉스 KRX 시세 + 나스닥 ADR(SKHY) + 원/달러 환율 수집기.

KIS API 키 없이 동작한다 — yfinance가 KRX 대형주(.KS 접미사)와
나스닥 ADR, 외환 티커(KRW=X)를 모두 커버하기 때문이다.
수급(투자자별 매매동향)은 여전히 KIS가 필요하다 — 이건 시세만.

티커:
    000660.KS  SK하이닉스 (KRX)
    005930.KS  삼성전자 (KRX)
    ^KS11      코스피 지수 (analysis/signals.py의 KSJ_PANIC_DAY·KSJ_BREADTH가 씀)
    SKHY       SK hynix ADR (Nasdaq, 2026년 상장)
    KRW=X      USD/KRW 환율 (1달러 = 몇 원)

환율은 daily_price 테이블에 ticker='USDKRW'로 같이 넣는다 — 그냥 숫자 시계열이라
가격 테이블 재사용이 스키마 추가보다 낫다고 판단했다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yfinance as yf  # noqa: E402

from db import store  # noqa: E402

TICKERS = {
    "000660.KS": "000660",
    "005930.KS": "005930",
    "069500.KS": "069500",  # KODEX 200 — executor 단순 보유 비교 기준(config/executor.yaml)
    "^KS11": "KOSPI",
    "SKHY": "SKHY",
    "KRW=X": "USDKRW",
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
            store.log_run("kr_market", "fail", 0, f"{symbol}: {e}")
    store.log_run("kr_market", "ok", total)
    return total


if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "1y"
    print(f"[kr_market] period={period}")
    print(f"완료: {collect_all(period)} rows")
