"""
data/manual/에 사용자가 직접 넣어둔 KRX 공식 종가 CSV와 이 프로젝트 DB
(yfinance 기반)의 값을 **대조만** 한다 — 어느 쪽이 맞는지 자동으로 판단해
고치지 않는다(공통 규칙).

배경: 2026-10-05 세션에서 000660의 2001-11-12에 +32.08% 단일 거래일 변동
(당시 KRX 일일 가격제한 ±15% 추정을 넘는 값)이 yfinance 기반 DB에서
발견됐고, 원인(데이터 벤더 보정 vs 실제 거래정지 재개 vs 다른 사유)을
확정하지 못한 채로 남아 있다(CLAUDE.md "미완" 참고). 이 스크립트는 그
의문을 풀기 위한 도구다 — data/manual/에 공식 데이터를 넣으면 같은 날짜의
수익률을 DB와 나란히 비교해서 보여준다.

data/manual/*.csv를 전부 훑어서 필요한 날짜를 포함한 파일을 찾는다(형식은
data/manual/README.md 참고). 비교 대상 CSV가 없으면 "비교 불가"라고만
표시한다(지어내지 않음).

사용:
    python scripts/verify_outlier.py                       # 기본: 알려진 이상치(000660, 2001-11-12) 확인
    python scripts/verify_outlier.py --ticker 005930 --date 2020-03-19  # 임의 날짜도 확인 가능
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import price_limits as pl  # noqa: E402
from db import store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANUAL_DIR = ROOT / "data" / "manual"
DEFAULT_TOLERANCE_PCT = 0.05  # %p 단위 — 이 이상 차이나면 "불일치"로 표시

DATE_COL_CANDIDATES = ["date", "일자", "날짜"]
CLOSE_COL_CANDIDATES = ["close", "종가"]

# 지어낸 값이 아니라 2026-10-05 세션에서 DB(yfinance) 데이터로 실제 발견한
# 이상치 — CLAUDE.md "완료 (2026-10-05 세션 4차)" 참고.
KNOWN_OUTLIERS = [
    {
        "ticker": "000660", "date": "2001-11-12",
        "note": "2026-10-05 세션에서 발견(+32.08% 단일 거래일, 당시 가격제한 "
                "±15% 추정 초과). 원인 미확정 — 공식 데이터로 대조 필요.",
    },
]


def _find_col(columns, candidates: list[str]) -> str | None:
    lowered = {str(c).strip().lower(): c for c in columns}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    return None


def load_manual_csv_closes(csv_path: Path) -> pd.Series:
    """
    date 인덱스 -> 종가 Series(오름차순). 날짜/종가 열을 못 찾으면 빈
    Series를 돌려준다(값을 지어내지 않음 — 파일 형식이 안 맞으면 "데이터
    없음"으로 처리되게 호출부에 맡긴다).
    """
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return pd.Series(dtype=float)
    date_col = _find_col(df.columns, DATE_COL_CANDIDATES)
    close_col = _find_col(df.columns, CLOSE_COL_CANDIDATES)
    if date_col is None or close_col is None:
        return pd.Series(dtype=float)
    dates = pd.to_datetime(df[date_col], errors="coerce")
    closes = pd.to_numeric(
        df[close_col].astype(str).str.replace(",", "", regex=False), errors="coerce"
    )
    s = pd.Series(closes.to_numpy(), index=dates)
    return s[s.index.notna()].dropna().sort_index()


def _return_on(closes: pd.Series, date: str) -> float | None:
    """date의 전거래일(closes 안에서 그 이전 가장 가까운 날짜) 종가 대비
    수익률(%). 필요한 값이 없으면 None(지어내지 않음)."""
    if closes.empty:
        return None
    ts = pd.Timestamp(date)
    if ts not in closes.index:
        return None
    prior = closes.index[closes.index < ts]
    if len(prior) == 0:
        return None
    prev_date = prior.max()
    prev_close, today_close = closes.loc[prev_date], closes.loc[ts]
    if prev_close == 0:
        return None
    return (today_close / prev_close - 1) * 100


def db_return_on(ticker: str, date: str, path: Path | None = None) -> float | None:
    return _return_on(store.get_prices(ticker, path=path), date)


@dataclass
class OutlierCheck:
    ticker: str
    date: str
    db_return_pct: float | None
    csv_return_pct: float | None
    csv_file: str | None
    match: bool | None  # None = 한쪽 데이터가 없어서 비교 불가
    diff_pct_points: float | None


def find_manual_return(ticker: str, date: str, manual_dir: Path) -> tuple[float | None, str | None]:
    """manual_dir의 *.csv를 훑어서 해당 날짜의 수익률을 계산할 수 있는 첫
    파일을 쓴다. 파일 이름으로 종목을 구분하지 않는다(README 참고 — 종목당
    파일 하나를 권장하지만 강제하지 않음)."""
    if not manual_dir.exists():
        return None, None
    for p in sorted(manual_dir.glob("*.csv")):
        r = _return_on(load_manual_csv_closes(p), date)
        if r is not None:
            return r, p.name
    return None, None


def check_outlier(
    ticker: str, date: str, manual_dir: Path = DEFAULT_MANUAL_DIR,
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT, db_path: Path | None = None,
) -> OutlierCheck:
    db_ret = db_return_on(ticker, date, path=db_path)
    csv_ret, csv_file = find_manual_return(ticker, date, manual_dir)
    if db_ret is None or csv_ret is None:
        return OutlierCheck(ticker, date, db_ret, csv_ret, csv_file, None, None)
    diff = float(db_ret - csv_ret)
    return OutlierCheck(ticker, date, db_ret, csv_ret, csv_file, bool(abs(diff) <= tolerance_pct), diff)


def _print_result(r: OutlierCheck, note: str | None = None) -> None:
    print(f"\n[{r.ticker}] {r.date}")
    if note:
        print(f"  참고: {note}")
    print(f"  DB 수익률:  {f'{r.db_return_pct:+.2f}%' if r.db_return_pct is not None else '데이터 없음'}")
    if r.csv_file:
        print(f"  CSV({r.csv_file}) 수익률: {r.csv_return_pct:+.2f}%")
    else:
        print("  CSV 수익률: 해당 날짜를 포함한 CSV 없음 — data/manual/에 공식 데이터를 넣으면 비교됩니다.")
    if r.match is None:
        print("  -> 비교 불가(한쪽 데이터 없음)")
    elif r.match:
        print(f"  -> 일치 (차이 {r.diff_pct_points:+.3f}%p)")
    else:
        print(f"  -> 불일치! 차이 {r.diff_pct_points:+.3f}%p — 자동으로 고치지 않습니다. 원본을 직접 확인하세요.")


# KRX 개별종목만 일일 가격제한 대상이다(지수·ETF·선물·FX·ADR은 해당 없음) —
# 이 프로젝트가 수집하는 KRX 상장 보통주는 지금 이 둘뿐이다
# (collectors/kr_market.py의 TICKERS 참고, KOSPI·SKHY·USDKRW는 제외).
KRX_EQUITY_TICKERS = ["000660", "005930"]


def scan_all_tickers_for_limit_exceedances(
    tickers: list[str] | None = None, tolerance_pct: float = 0.5, db_path: Path | None = None,
) -> list[pl.LimitExceedance]:
    """
    tickers의 일일 수익률을 날짜별 가격제한 테이블(analysis/price_limits.py)
    과 대조한다. 기본값은 KRX_EQUITY_TICKERS(실제 가격제한이 적용되는
    KRX 개별종목만) — DB의 모든 티커를 그대로 돌리면 지수·선물·FX처럼
    가격제한 자체가 없는 티커까지 "위반"으로 잘못 걸릴 수 있어서다(실제로
    이 스캔을 전체 티커로 처음 돌려봤을 때 US10Y·SILVER가 걸렸는데, 둘 다
    KRX 개별종목이 아니라 가격제한 대상이 아니다 — 그래서 기본 대상을
    좁혔다). DB에 아직 없는 티커는 조용히 건너뛴다.
    """
    tickers = tickers if tickers is not None else KRX_EQUITY_TICKERS
    closes_by_ticker = {t: store.get_prices(t, path=db_path) for t in tickers}
    closes_by_ticker = {t: s for t, s in closes_by_ticker.items() if not s.empty}
    return pl.find_exceedances(closes_by_ticker, tolerance_pct=tolerance_pct)


def _print_exceedances(exceedances: list[pl.LimitExceedance]) -> None:
    print(f"\n=== 가격제한 초과 스캔 ({pl.PRICE_LIMIT_SCHEDULE[0].effective_on} 이후 구간만 판정) ===")
    if not exceedances:
        print("  판정 가능한 구간에서 가격제한 초과 없음.")
        return
    for e in sorted(exceedances, key=lambda x: x.date):
        print(f"  [{e.ticker}] {e.date}: {e.return_pct:+.2f}% (그 시점 제한 ±{e.limit_pct:.0f}%)")


def main() -> None:
    p = argparse.ArgumentParser(description="DB 값과 data/manual/의 공식 KRX CSV 값을 대조(자동 보정 없음)")
    p.add_argument("--ticker", default=None)
    p.add_argument("--date", default=None)
    p.add_argument("--manual-dir", default=str(DEFAULT_MANUAL_DIR))
    p.add_argument("--scan-limits", action="store_true", help="KRX 개별종목(KRX_EQUITY_TICKERS)을 날짜별 가격제한 테이블과 대조 스캔")
    p.add_argument("--scan-limits-all", action="store_true", help="--scan-limits를 DB의 모든 티커로 확장(지수·ETF 등 포함 — 오탐 주의)")
    args = p.parse_args()

    if bool(args.ticker) != bool(args.date):
        p.error("--ticker와 --date는 함께 지정해야 합니다.")

    store.init_db()
    manual_dir = Path(args.manual_dir)
    if not manual_dir.exists():
        print(f"{manual_dir}가 없습니다 — data/manual/README.md를 참고해 공식 CSV를 넣으면 비교가 가능합니다.")

    targets = [{"ticker": args.ticker, "date": args.date, "note": None}] if args.ticker else KNOWN_OUTLIERS

    for t in targets:
        result = check_outlier(t["ticker"], t["date"], manual_dir=manual_dir)
        _print_result(result, t.get("note"))

    if args.scan_limits or args.scan_limits_all:
        scan_tickers = store.list_tickers() if args.scan_limits_all else None
        _print_exceedances(scan_all_tickers_for_limit_exceedances(tickers=scan_tickers))

    print(
        "\n이 스크립트는 비교만 합니다 — 불일치가 나와도 DB 값을 자동으로 "
        "고치지 않습니다. 매수·매도 추천이 아닙니다."
    )


if __name__ == "__main__":
    main()
