"""
MCP 툴의 실제 로직. `mcp` 패키지를 import하지 않는다 — 그래서 mcp SDK가
설치돼 있지 않은 환경에서도 이 모듈만은 그대로 테스트할 수 있다.

server.py가 이 함수들을 @mcp.tool()로 얇게 감싼다. 로직과 프로토콜 배선을
분리해둔 이유는 그거 하나다.

설계 원칙 재확인 (CLAUDE.md):
  - 여기엔 조회 함수만 있다. 매매·주문 관련 함수를 추가하지 않는다.
  - 모든 응답에 표본 수(samples/n)를 포함한다. 숨기지 않는다.
  - 어떤 함수도 "오를 것" 류의 방향성 문구를 만들지 않는다 — dict의 값은
    analysis 모듈이 계산한 숫자 그대로이고, note 필드로 그 사실을 못박아둔다.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import (  # noqa: E402
    adr,
    backtest,
    calendar_rules as cal,
    correlation as corr,
    frameworks,
    rule_backtest as rbt,
    sentiment,
    strategies as strat,
)
from config import settings  # noqa: E402
from db import store  # noqa: E402

WATCH_US = ["NVDA", "MU", "AMD", "TSM"]
ALL_US_TICKERS = ["SOX", *WATCH_US, "VIX"]
HYNIX = "000660"
ADR_TICKER = "SKHY"
FX_TICKER = "USDKRW"

RULE_BACKTEST_MIN_ROWS = 250  # 대략 1년 거래일. scripts/backtest_strategies.py와 같은 기준값이지만
# 모듈 간 의존을 안 만드는 기존 관행(db/store.py MIN_SAMPLES_CLAIMS 주석 참고)대로 독립적으로 둔다.

STRATEGY_FNS = {
    "ma_cross": strat.ma_cross,
    "rsi_reversion": strat.rsi_reversion,
    "bollinger_reversion": strat.bollinger_reversion,
    "macd_cross": strat.macd_cross,
}

OBSERVATION_NOTE = "관측값입니다. 향후 주가 방향을 예측하지 않습니다."


class DataNotFoundError(ValueError):
    """요청한 티커의 시세가 DB에 없을 때. MCP 클라이언트에는 평범한 에러 메시지로 전달된다."""


def _require_prices(ticker: str):
    store.init_db()
    s = store.get_prices(ticker)
    if s.empty:
        raise DataNotFoundError(
            f"'{ticker}' 시세 데이터가 없습니다. "
            f"먼저 'python collectors/us_market.py 1y' 그리고 'python collectors/kr_market.py 1y'로 "
            f"수집하세요. 현재 사용 가능한 종목: {', '.join(ALL_US_TICKERS)}, {HYNIX}, {ADR_TICKER} "
            f"(KIS 연동 전에도 시세는 전부 yfinance로 받아옴)."
        )
    return s


def _require_ohlc(ticker: str):
    """get_rule_backtest 전용: 체결가(시가) 계산에 필요한 시가+종가를 함께 확보한다."""
    store.init_db()
    ohlc = store.get_ohlc(ticker)
    close = ohlc["close"].dropna().sort_index()
    if len(close) < RULE_BACKTEST_MIN_ROWS:
        raise DataNotFoundError(
            f"'{ticker}' 시세가 {len(close)}일뿐입니다 (규칙 백테스트 권장 최소 "
            f"{RULE_BACKTEST_MIN_ROWS}일). 먼저 'python collectors/kr_market.py 5y'"
            "(또는 max)로 더 긴 기간을 수집하세요."
        )
    open_ = ohlc["open"].reindex(close.index)
    return close, open_


def _records(df) -> list[dict]:
    """DataFrame.to_dict('records')가 SQL NULL을 NaN으로 넘기는 문제를 막는다."""
    if df.empty:
        return []
    return df.astype(object).where(df.notna(), None).to_dict("records")


def get_correlation(ticker: str, benchmark: str = "SOX", window: int = 60) -> dict:
    """
    두 종목 일간수익률의 상관계수·베타·최대상관 시차를 계산한다.

    Args:
        ticker: 대상 종목 (NVDA, MU, AMD, TSM 등)
        benchmark: 비교 기준 종목 (기본값 SOX)
        window: 최근 몇 거래일을 볼지 (기본값 60)
    """
    a = _require_prices(ticker)
    b = _require_prices(benchmark)
    c = corr.correlation(a, b, window=window)
    beta = corr.beta(a, b, window=window)
    lags = corr.lagged_correlation(b, a, max_lag=3, window=window)
    best_lag, best = max(
        lags.items(),
        key=lambda kv: abs(kv[1].coefficient) if kv[1].coefficient == kv[1].coefficient else -1,
    )
    return {
        "ticker": ticker,
        "benchmark": benchmark,
        "window": window,
        "correlation": round(c.coefficient, 4) if c.coefficient == c.coefficient else None,
        "samples": c.samples,
        "reliable": c.reliable,
        "min_samples_recommended": corr.MIN_SAMPLES,
        "beta": round(beta, 4) if beta == beta else None,
        "best_lag_days": best_lag,
        "best_lag_correlation": round(best.coefficient, 4) if best.coefficient == best.coefficient else None,
        "note": OBSERVATION_NOTE,
    }


def get_divergence_days(
    ticker: str, benchmark: str = "SOX", window: int = 120,
    threshold: float = 2.0, limit: int = 10,
) -> dict:
    """
    평소 상관관계에서 벗어난 날짜를 찾는다. 최신순으로 반환한다.
    원인은 제공하지 않는다 — 날짜와 괴리 크기(z)만 반환한다.

    Args:
        ticker: 대상 종목
        benchmark: 비교 기준 종목
        window: 베타 계산에 쓸 기간(거래일)
        threshold: 이 값(표준편차 배수) 이상 벗어난 날만 포함
        limit: 최대 반환 개수
    """
    a = _require_prices(ticker)
    b = _require_prices(benchmark)
    d = corr.divergence(a, b, window=window, threshold=threshold)
    rows = list(d.tail(limit).iloc[::-1].iterrows())
    return {
        "ticker": ticker,
        "benchmark": benchmark,
        "window": window,
        "threshold": threshold,
        "count": len(rows),
        "days": [
            {
                "date": idx.date().isoformat(),
                "ticker_change_pct": round(r["target"] * 100, 2),
                "benchmark_change_pct": round(r["benchmark"] * 100, 2),
                "z": round(r["z"], 2),
            }
            for idx, r in rows
        ],
        "note": "개별 재료가 있었을 가능성이 있는 날짜 목록입니다. 원인이나 향후 방향은 포함하지 않습니다.",
    }


def get_calendar_events(days: int = 30) -> dict:
    """
    앞으로 N일 안의 만기일·자사주매입 기간·실적 발표 등 확정된 일정만 반환한다.
    전망이나 추정치는 포함하지 않는다 — analysis/calendar_rules.py에 수동 등록된
    공시 기반 사실만 나온다.

    Args:
        days: 오늘부터 며칠 뒤까지 볼지 (기본값 30)
    """
    today = date.today()
    events = cal.upcoming(today, days)
    return {
        "from": today.isoformat(),
        "days": days,
        "events": [
            {"date": e.date.isoformat(), "kind": e.kind, "label": e.label, "note": e.note}
            for e in events
        ],
    }


def get_backtest(
    ticker: str, event: str = "quad_witching",
    entry_offset: int = -10, exit_offset: int = -1, years_back: int = 3,
) -> dict:
    """
    캘린더 이벤트를 기준으로 한 상대일 구간의 과거 수익률 분포를 계산한다.
    거래비용(수수료+세금)은 반영하지 않는다. 표본이 30개 미만이면
    reliable=False가 붙는다 — 이 값을 반드시 함께 전달할 것.

    Args:
        ticker: 대상 종목
        event: 현재는 "quad_witching"(미국 네 마녀의 날)만 지원
        entry_offset: 이벤트 기준 진입 상대 거래일 (기본 -10)
        exit_offset: 이벤트 기준 청산 상대 거래일 (기본 -1)
        years_back: 몇 년 전 이벤트까지 포함할지 (기본 3)
    """
    if event != "quad_witching":
        raise ValueError("현재는 event='quad_witching'만 지원합니다.")
    s = _require_prices(ticker)
    today = date.today()
    event_dates = cal.historical_quad_witching(date(today.year - years_back, 1, 1), today)
    res = backtest.event_window_returns(
        s, event_dates, entry_offset=entry_offset, exit_offset=exit_offset,
        label=f"{ticker}: 네 마녀의 날 D{entry_offset}~D{exit_offset}",
    )
    return {
        "ticker": ticker,
        "event": event,
        "entry_offset": entry_offset,
        "exit_offset": exit_offset,
        "n": res.n,
        "win_rate_pct": round(res.win_rate, 2) if res.n else None,
        "mean_return_pct": round(res.mean_return, 2) if res.n else None,
        "worst_pct": round(res.worst, 2) if res.n else None,
        "best_pct": round(res.best, 2) if res.n else None,
        "std_pct": round(res.std, 2) if res.n > 1 else None,
        "reliable": res.reliable,
        "min_samples_recommended": backtest.MIN_SAMPLES,
        "note": "과거 관측치입니다. 향후 결과를 보장하지 않으며 거래비용은 반영되지 않았습니다.",
    }


def get_collection_status() -> dict:
    """수집 파이프라인 상태를 반환한다: 최근 배치 로그, KIS 연동 여부, 감시 종목 목록."""
    store.init_db()
    logs = store.recent_logs(5)
    return {
        "kis_ready": settings.kis_ready,
        "watch_kr": settings.watch_kr,
        "watch_us": ALL_US_TICKERS,
        "recent_runs": _records(logs),
    }


def get_adr_premium() -> dict:
    """
    SK하이닉스 ADR(SKHY)과 KRX 보통주(000660) 사이의 가격 차이(프리미엄)를 계산한다.
    전환비율 1 ADS = 보통주 1/10주 기준. 매수·매도 신호가 아니라 두 시장 간
    가격 차이를 나타내는 관측값이다. 프리미엄이 향후 좁혀질지 벌어질지는 알 수 없다.
    """
    krx = _require_prices(HYNIX)
    adr_s = _require_prices(ADR_TICKER)
    fx = _require_prices(FX_TICKER)
    p = adr.premium(
        krx_close_krw=float(krx.iloc[-1]), adr_close_usd=float(adr_s.iloc[-1]),
        usd_krw_rate=float(fx.iloc[-1]),
    )
    return {
        "krx_close_krw": float(krx.iloc[-1]),
        "adr_close_usd": float(adr_s.iloc[-1]),
        "usd_krw_rate": float(fx.iloc[-1]),
        "ratio": p.ratio,
        "parity_usd": round(p.parity_usd, 4),
        "premium_pct": round(p.premium_pct, 2),
        "note": "패리티는 000660 종가를 전환비율로 환산한 이론가. "
                "실제가와의 차이는 거래시간·환율·유동성 차이에서도 발생하며, "
                + OBSERVATION_NOTE,
    }


def get_market_sentiment() -> dict:
    """
    VIX(CBOE 변동성지수) 기반 시장 심리 게이지. CNN Fear & Greed Index와는
    다른 지수다 — VIX 하나만으로 방법론을 투명하게 계산한 자체 대체 지표.
    """
    vix = _require_prices("VIX")
    g = sentiment.vix_gauge(vix)
    return {
        "vix_level": g.vix_level,
        "percentile_in_window": g.percentile,
        "gauge_score": g.score,
        "category": g.category,
        "samples": g.samples,
        "reliable": g.reliable,
        "note": "CNN Fear&Greed Index를 그대로 가져온 것이 아니다. CBOE VIX의 "
                "최근 분포 내 백분위를 뒤집은 자체 게이지. " + OBSERVATION_NOTE,
    }


def get_news(ticker: str | None = None, limit: int = 10) -> dict:
    """
    저장된 뉴스 헤드라인을 반환한다. confidence='rumor'인 항목은 출처가
    검증되지 않은 소문이다 — 반드시 함께 전달할 것. 본문 요약이나 해석은 없다.

    Args:
        ticker: 특정 종목만 (예: 000660). 생략하면 전체.
        limit: 최대 반환 개수
    """
    store.init_db()
    df = store.get_news(ticker=ticker, limit=limit)
    return {
        "ticker": ticker,
        "count": len(df),
        "items": _records(df),
        "note": "confidence='rumor'는 미확인 소문입니다. 사실인 것처럼 전달하지 마세요.",
    }


def get_framework_check(
    ticker: str = HYNIX, benchmark: str = "SOX",
    event: str = "quad_witching", years_back: int = 3,
) -> dict:
    """
    투자 프레임워크 렌즈 체크. 실존 인물의 발언을 인용하거나 재현하지 않는다 —
    공개적으로 알려진 프레임워크 이름만 빌려서, 지금 데이터로 확인 가능한
    사실만 충족/미충족/확인 필요로 분류한다. 재무데이터가 필요한 가치·성장·
    밸류에이션 렌즈는 데이터가 없어 스텁으로 반환된다 (지어내지 않는다).

    Args:
        ticker: 대상 종목 (기본 000660)
        benchmark: 사이클 렌즈에서 베타 계산에 쓸 기준 종목
        event: 리스크 렌즈 백테스트에 쓸 이벤트 (현재 quad_witching만 지원)
        years_back: 백테스트에 포함할 과거 연수
    """
    s = _require_prices(ticker)
    bench = _require_prices(benchmark)
    today = date.today()

    c = corr.correlation(s, bench, window=corr.MIN_SAMPLES * 2)
    b = corr.beta(s, bench, window=corr.MIN_SAMPLES * 2)

    adr_premium_pct = None
    try:
        adr_s = _require_prices(ADR_TICKER)
        fx = _require_prices(FX_TICKER)
        adr_premium_pct = adr.premium(float(s.iloc[-1]), float(adr_s.iloc[-1]), float(fx.iloc[-1])).premium_pct
    except DataNotFoundError:
        pass

    event_dates = cal.historical_quad_witching(date(today.year - years_back, 1, 1), today)
    res = backtest.event_window_returns(s, event_dates, -10, -1, label=f"{ticker}: 네 마녀의 날 D-10~D-1")

    today_events = cal.events_for(today)
    has_period_event = any(e.kind == "period" for e in today_events)
    earnings_days = next((e.date for e in cal.upcoming(today, 14) if e.kind == "earnings"), None)
    earnings_within = (earnings_days - today).days if earnings_days else None

    lenses = [
        frameworks.risk_lens(res.n, res.reliable, res.worst if res.n else None, adr_premium_pct),
        frameworks.cycle_lens(has_period_event, earnings_within, b, c.reliable),
        frameworks.fact_checker_lens([
            {"label": f"{ticker} vs {benchmark} 상관계수", "value": c.coefficient, "samples": c.samples},
            {"label": "이벤트 백테스트 최대손실", "value": res.worst if res.n else None, "samples": res.n},
            {"label": "ADR 프리미엄", "value": adr_premium_pct, "samples": None},
        ]),
        frameworks.VALUE_LENS_STUB(),
        frameworks.GROWTH_LENS_STUB(),
        frameworks.VALUATION_LENS_STUB(),
    ]

    return {
        "ticker": ticker,
        "lenses": [
            {
                "lens": lr.lens,
                "data_available": lr.data_available,
                "findings": [
                    {"criterion": f.criterion, "status": f.status, "detail": f.detail}
                    for f in lr.findings
                ],
            }
            for lr in lenses
        ],
        "note": "실존 인물의 1인칭 발언이 아닙니다. 프레임워크 이름만 참고한 사실 체크입니다. "
                + OBSERVATION_NOTE,
    }


def get_claim_scoreboard(recent_limit: int = 10) -> dict:
    """
    전문가 발언/소문(claims)의 사후 채점 결과를 반환한다. 이미 지난 발언이
    목표가·기간 기준으로 맞았는지 집계한 것이며, 향후 발언의 적중을 보장하지
    않는다. confidence='rumor'인 항목은 미확인 소문이다 — recent 항목에서
    함께 전달할 것. 채점 자체는 scripts/grade_claims.py 배치가 수행하며,
    이 함수는 그 결과를 읽기만 한다(조회 전용).

    Args:
        recent_limit: 최근 채점 결과를 몇 건까지 포함할지 (기본 10)
    """
    store.init_db()
    board = store.get_claim_scoreboard(recent_limit=recent_limit)
    return {
        **board,
        "note": "사후 채점 결과입니다 — 과거 발언이 목표가·기간 기준으로 맞았는지 "
                "집계한 것이며 향후 적중을 보장하지 않습니다. " + OBSERVATION_NOTE,
    }


def get_agent_analysis(ticker: str = HYNIX) -> dict:
    """
    scripts/run_agents.py가 배치로 미리 생성해 DB에 캐싱해둔 에이전트 리서치
    요약을 읽는다. 이 함수 자체는 LLM API를 호출하지 않는다 — 캐싱된 게
    없으면 없다고만 알린다 (조회 전용 원칙, 이 툴을 호출해도 비용이 들지 않음).

    Args:
        ticker: 대상 종목 (기본 000660)
    """
    store.init_db()
    row = store.get_latest_agent_analysis(ticker)
    if row is None:
        return {
            "ticker": ticker,
            "available": False,
            "note": "아직 생성된 분석이 없습니다. 'python scripts/run_agents.py --ticker "
                    f"{ticker}'로 배치를 먼저 실행하세요 (Anthropic API 비용 발생).",
        }
    return {
        "ticker": ticker,
        "available": True,
        "run_date": row["run_date"],
        "model": row["model"],
        "narrative": row["narrative"],
        "tool_calls": json.loads(row["tool_calls"]) if row["tool_calls"] else [],
        "generated_at": row["created_at"],
        "note": "배치 작업 시점에 생성된 캐시된 분석입니다. 실시간이 아니며, "
                "방향성 예측이 아니라 그 시점 데이터에 대한 서술입니다. " + OBSERVATION_NOTE,
    }


def get_rule_backtest(
    ticker: str = HYNIX, strategy: str = "ensemble",
    threshold: float = strat.DEFAULT_ENSEMBLE_THRESHOLD,
) -> dict:
    """
    기술적 지표 규칙(ma_cross/rsi_reversion/bollinger_reversion/macd_cross/ensemble)을
    과거 데이터에 그대로 적용했을 때의 성과를 측정한다. **매매 신호가 아니라 과거
    측정**이다 — 이 함수는 주문을 실행하지 않고(이 프로젝트는 매매 기능을 의도적으로
    구현하지 않았다), "지금 사라/팔아라" 같은 판단도 만들어내지 않는다.

    체결 가정: t일 종가로 만든 신호는 t+1일 시가(없으면 종가)에 체결된다고 본다.
    거래비용(매수/매도 수수료 + 매도세)이 반영돼 있다 — 기본값은 참고용 근사치이며
    실제 요율은 증권사·연도별로 다르다. 거래 횟수(n) < 30이면 reliable=False다 —
    이 경우 이 값을 근거로 어떤 판단도 내리지 말고 반드시 사용자에게 알릴 것.
    기간을 앞 70%(in_sample)/뒤 30%(out_of_sample)로 나눈 결과도 함께 반환한다.

    Args:
        ticker: 대상 종목 (기본 000660)
        strategy: ma_cross | rsi_reversion | bollinger_reversion | macd_cross | ensemble
        threshold: strategy='ensemble'일 때만 쓰는 보유 임계값 (기본 0.5, 4개 중 2개 이상 동의)
    """
    if strategy != "ensemble" and strategy not in STRATEGY_FNS:
        raise ValueError(
            f"알 수 없는 strategy '{strategy}'. 선택 가능: "
            f"{', '.join([*STRATEGY_FNS, 'ensemble'])}"
        )
    close, open_ = _require_ohlc(ticker)
    holding = (
        strat.ensemble(close, threshold=threshold) if strategy == "ensemble"
        else STRATEGY_FNS[strategy](close)
    )
    results = rbt.backtest_rule(close, holding, open_, label=strategy)

    def _n(x: float) -> float | None:
        return round(x, 2) if x == x else None  # NaN -> None (표 대신 "nan" 문자열이 찍히는 버그 방지)

    def _window(r):
        return {
            "start": r.start.isoformat() if r.start else None,
            "end": r.end.isoformat() if r.end else None,
            "n": r.n,
            "reliable": r.reliable,
            "total_return_pct": _n(r.total_return_pct),
            "cagr_pct": _n(r.cagr_pct),
            "mdd_pct": _n(r.mdd_pct),
            "win_rate_pct": _n(r.win_rate),
            "avg_win_pct": _n(r.avg_win),
            "avg_loss_pct": _n(r.avg_loss),
            "worst_trade_pct": _n(r.worst),
            "exposure_pct": _n(r.exposure_pct),
        }

    return {
        "ticker": ticker,
        "strategy": strategy,
        "threshold": threshold if strategy == "ensemble" else None,
        "min_samples_recommended": rbt.MIN_SAMPLES,
        "commission_rate": rbt.DEFAULT_COMMISSION_RATE,
        "tax_rate": rbt.DEFAULT_TAX_RATE,
        "full": _window(results["full"]),
        "in_sample": _window(results["in_sample"]),
        "out_of_sample": _window(results["out_of_sample"]),
        "note": "과거 데이터에 대한 측정 결과입니다. 매수·매도 추천이 아니며 향후 수익을 "
                "보장하지 않습니다. 거래비용은 참고용 근사치이며 실제 요율은 "
                "증권사·연도별로 다릅니다. " + OBSERVATION_NOTE,
    }


def get_price_summary(ticker: str = HYNIX) -> dict:
    """
    최근 종가와 과거 대비 변화(관측값)만 돌려준다 — 20·60·120거래일 수익률, 52주(252거래일) 고가·저가 대비 위치,
    20·60일 이동평균 대비 위치. 향후 방향을 말하지 않는다.
    """
    close = _require_prices(ticker).sort_index()
    last = float(close.iloc[-1])

    def ret(n: int):
        return round((last / float(close.iloc[-1 - n]) - 1) * 100, 2) if len(close) > n else None

    def vs_sma(n: int):
        return round((last / float(close.iloc[-n:].mean()) - 1) * 100, 2) if len(close) >= n else None

    window = close.iloc[-252:]
    return {
        "ticker": ticker, "as_of": close.index[-1].date().isoformat(), "close": last,
        "return_20d_pct": ret(20), "return_60d_pct": ret(60), "return_120d_pct": ret(120),
        "vs_sma20_pct": vs_sma(20), "vs_sma60_pct": vs_sma(60),
        "from_52w_high_pct": round((last / float(window.max()) - 1) * 100, 2),
        "from_52w_low_pct": round((last / float(window.min()) - 1) * 100, 2),
        "samples": len(window), "note": OBSERVATION_NOTE,
    }


def get_rates_summary() -> dict:
    """
    미국 10년물 금리(^TNX, 수집된 값)의 현재 수준·20/60거래일 변화와, 수동 입력된 금리 사이클 단계(있으면).
    수동 입력이 없으면 "데이터 없음" — 지어내지 않는다.
    """
    store.init_db()
    tnx = _require_prices("US10Y").sort_index()
    last = float(tnx.iloc[-1])

    def chg(n: int):
        return round(last - float(tnx.iloc[-1 - n]), 3) if len(tnx) > n else None

    stage = store.get_manual_timeline("rate_stage")
    latest_stage = None
    if not stage.empty:
        row = stage.sort_values(stage.columns[0]).iloc[-1].to_dict()
        latest_stage = {k: (str(v) if v == v else None) for k, v in row.items()}
    return {
        "us10y_as_of": tnx.index[-1].date().isoformat(), "us10y_pct": round(last, 3),
        "us10y_change_20d_pctp": chg(20), "us10y_change_60d_pctp": chg(60),
        "rate_stage_manual": latest_stage or "데이터 없음(scripts/add_manual.py rate-stage 미입력)",
        "note": OBSERVATION_NOTE,
    }
