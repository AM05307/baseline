"""
FastAPI 대시보드. 조회 전용.

설계 원칙 준수 (CLAUDE.md):
  1. 백테스트로 검증된 규칙만 매수/매도 기준가로 표시한다 — "오를 것 같다" 류의
     방향성 전망·확률은 여전히 만들지 않는다. 가격은 analysis/price_rules.py가
     기계적으로 계산하고, analysis/price_rule_backtest.passes()를 통과한
     규칙만(거래 100건↑·전체·최근1년 거래당평균 둘 다 양수) 가격을 노출한다.
  2. 매매 관련 엔드포인트를 두지 않는다 — 이 파일에는 GET만 있다.
  3. 표본 수를 항상 함께 보여준다.
  4. 최대손실을 승률과 같은 비중으로 보여준다.
  5. 뉴스/소문은 출처·confidence를 항상 노출한다. 'rumor'는 반드시 "미확인" 라벨.
  6. 프레임워크 렌즈는 실존 인물의 1인칭 발언을 만들지 않는다 — 프레임워크
     이름만 빌리고, 충족/미충족/확인 필요로만 분류한다 (analysis/frameworks.py).

실행:
    uvicorn web.main:app --reload
    (프로젝트 루트에서 실행할 것.)

먼저 데이터가 있어야 한다:
    python collectors/us_market.py 1y     # SOX·NVDA·MU·TSM·AMD·VIX
    python collectors/kr_market.py 1y     # 000660·SKHY·USDKRW (KIS 키 불필요)
    python collectors/news.py             # 뉴스 헤드라인
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from analysis import (  # noqa: E402
    adr,
    backtest,
    calendar_rules as cal,
    checklist,
    correlation as corr,
    event_study as evs,
    events as ev,
    frameworks,
    group_score as gs,
    move_attribution as mva,
    price_rule_backtest as prb,
    price_rules,
    risk_score as rs,
    rule_backtest as rbt,
    sentiment,
    signal_alignment as sa,
    signal_backtest as sb,
    strategies as strat,
    weighting as wt,
    what_if as wi,
)
from config import settings  # noqa: E402
from db import store  # noqa: E402
from web import charts  # noqa: E402

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(
    title="baseline",
    description="SK하이닉스·반도체 관계를 측정하는 개인 대시보드 (조회 전용, 예측 없음)",
)

HYNIX = "000660"
ADR = "SKHY"
FX = "USDKRW"
BENCHMARK = "SOX"
PEERS = ["SOX", "NVDA", "MU", "AMD", "TSM"]

CORR_WINDOW = 60
CHART_DAYS = 90
BACKTEST_YEARS_BACK = 3
NEWS_LIMIT = 8
MOVE_THRESHOLD_PCT = mva.DEFAULT_THRESHOLD_PCT  # 급변일 임계값(±%)
MOVE_BETA_WINDOW = mva.DEFAULT_BETA_WINDOW
MOVE_CARDS = 5
EVENT_STUDY_WINDOWS = (5, 20)  # 자사주 매입 종료 전후 N거래일 (설정값, 백테스트로 고른 값 아님)

# 6단계: 정밀 지표가 아니라 근사치로 대체한 신호 — 대시보드에 "근사 지표"
# 배지로 구분 표시한다. KSJ_BREADTH는 실제 KOSPI 상승종목비율 데이터가
# 없어서 반도체 2종목 가중평균으로 "반도체 제외 KOSPI"를 근사한다
# (scripts/seed_signals.py의 data_source 설명 참고, 확인 필요로 남겨둔 값).
APPROXIMATE_SIGNALS = {"KSJ_BREADTH"}
SIGNAL_RECENT_DAYS = 10  # 이 기간 안에 값이 없는 신호는 "현재 값 없음"으로 본다(점수 상한 경고용)
RULE_BACKTEST_MIN_ROWS = 250  # 대략 1년 거래일. 모듈 간 의존 안 만드는 기존 관행대로 독립 상수.
# scripts/backtest_strategies.py의 DEFAULT_START와 같은 이유로 둔 독립 상수
# (모듈 간 의존 안 만드는 기존 관행) — 000660은 2001년 전후 데이터에 이상치
# (2001-11-12 +32.08%, 당시 가격제한 초과)가 있어 2005년 이전은 뺀다.
PRICE_RULE_START = "2005-01-01"

RULE_STRATEGIES = [
    ("ma_cross", strat.ma_cross),
    ("rsi_reversion", strat.rsi_reversion),
    ("bollinger_reversion", strat.bollinger_reversion),
    ("macd_cross", strat.macd_cross),
    ("ensemble(0.5)", lambda s: strat.ensemble(s, threshold=0.5)),
    ("buy_and_hold", lambda s: pd.Series(1, index=s.index)),
]


def _pct_change(s):
    if len(s) < 2:
        return None
    prev, last = float(s.iloc[-2]), float(s.iloc[-1])
    return (last / prev - 1) * 100 if prev else None


def _normalize_to(s, base_value: float):
    """s를 base_value에서 시작하도록 정규화 — 같은 축에서 두 시리즈를 겹쳐 그릴 때 씀."""
    if s.empty:
        return s
    return s / float(s.iloc[0]) * base_value


def _records(df) -> list[dict]:
    """
    DataFrame.to_dict("records")는 SQL NULL을 NaN으로 넘긴다. NaN은 파이썬에서
    truthy라서 템플릿의 `{{ x or "기본값" }}` 패턴이 안 먹힌다 — "nan"이라는
    글자가 그대로 화면에 찍히는 버그가 실제로 났었다. 여기서 NaN을 None으로
    바꿔서 넘긴다.
    """
    if df.empty:
        return []
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _group_state(signals_df: pd.DataFrame, hynix: pd.Series) -> dict:
    """
    오늘의 상태(기본 방식, 2026-10-19): 신호 그룹 균등 가중치 + 기준 기간 백분위 단계(analysis/group_score).
    결정일 = 000660 마지막 거래일, 신호는 결정일 정렬 규칙(what_if·섀도와 같은 계산). 기준점은 한국 거래일(KOSPI 날짜)
    2005~2018 분포로 정한다. 기준 기간 데이터가 없으면 {"error": ...} — 기준점을 지어내지 않는다.
    """
    directional = signals_df[signals_df["direction"].isin(["risk", "opportunity"]) & (signals_df["enabled"] == 1)]
    if hynix.empty or directional.empty:
        return {"error": "시세 또는 신호 데이터 없음"}
    t = pd.Timestamp(hynix.sort_index().index[-1])
    grid = pd.DatetimeIndex(hynix.index).union(store.get_prices("KOSPI").index)
    values = {sid: store.get_signal_values(sid) for sid in directional["signal_id"]}
    is_on, _ = sa.align_signals(values, grid)
    try:
        st = wi.group_states(is_on, pd.DatetimeIndex([t]))
    except ValueError as e:
        return {"error": f"기준 기간 데이터 부족 — {e}"}
    thr, row = st.attrs["thresholds"], st.iloc[0]
    gscore = gs.group_scores(is_on.loc[[t]], gs.RISK_GROUPS).iloc[0]
    groups = []
    for g, sids in gs.RISK_GROUPS.items():
        with_value = [s for s in sids if s in is_on.columns and pd.notna(is_on.at[t, s])]
        groups.append({"name": g, "score_pct": None if pd.isna(gscore[g]) else float(gscore[g]) * 100,
                       "on": [s for s in with_value if is_on.at[t, s] == 1], "n_with_value": len(with_value),
                       "n_total": len(sids)})
    return {
        "decision_date": t.date().isoformat(), "risk_score": row["risk_score"], "opportunity_score": row["opportunity_score"],
        "stage": int(row["stage"]), "new_buy_status": row["new_buy_status"], "sell_stage": row["sell_stage"],
        "groups_with_data": int(row["groups_with_data"]), "groups": groups,
        "thresholds": [None if v == float("inf") else v for v in thr.values],
        "quantiles": list(thr.quantiles), "actual_shares": list(thr.actual_shares), "n_days": thr.n_days,
        "calibration": f"{thr.start} ~ {thr.end}",
    }


def build_dashboard_context() -> dict:
    """템플릿에 넘길 데이터를 조립하는 순수 함수. fastapi 타입에 의존하지 않는다."""
    store.init_db()
    today = date.today()

    ctx: dict = {
        "today": today.isoformat(),
        "min_samples": corr.MIN_SAMPLES,
        "kis_ready": settings.kis_ready,
        "watch_kr": settings.watch_kr,
        "today_events": cal.events_for(today),
        "upcoming_events": cal.upcoming(today, 45),
        "has_data": False,
        "kpi": {}, "main_chart_svg": "", "peers": [], "correlations": [],
        "adr": None, "sentiment": None, "gauge_svg": "",
        "news": [], "backtest": None, "lenses": [], "rule_backtest": None,
        "price_rule_signals": None, "analyst_views": [],
        "risk_state": None, "score_ceiling": None, "expert_rows": [], "checklist": checklist.CHECKLIST,
        "thematic_watchlist": cal.THEMATIC_WATCHLIST,
        "recent_logs": [], "agent_analysis": None, "claim_scoreboard": None,
        "move_cards": [], "move_meta": None,
        "watch_events": [], "event_study": None,
    }

    # 전문가 발언/소문 채점 현황. 000660 시세 유무와 무관하게 항상 보여준다
    # (claims는 별도 테이블이라 has_data 여부에 종속되지 않는다).
    ctx["claim_scoreboard"] = store.get_claim_scoreboard(recent_limit=8)

    # 애널리스트/의견 추적: 사람(who)별 최근 의견 1건 + 그 이후 실제 주가
    # 변화(price_then 대비 최신 종가). 채점(grade_claims.py) 완료 여부와
    # 무관하게 보여준다 — claim_scoreboard와 마찬가지로 항상 계산 가능해야
    # 한다. "신호"가 아니라 "기록"이다(원칙 5) — stance/affiliation은 분류
    # 라벨일 뿐, summary는 여전히 본인이 요약한 한 줄(원문 인용 금지).
    latest_claims_df = store.get_latest_claims_by_person(HYNIX)
    if not latest_claims_df.empty:
        latest_claims_df = latest_claims_df.sort_values("said_on", ascending=False)
    hynix_for_views = store.get_prices(HYNIX)
    latest_price_for_views = float(hynix_for_views.iloc[-1]) if not hynix_for_views.empty else None
    analyst_views = []
    for row in _records(latest_claims_df):
        price_then = row.get("price_then")
        pct_change_since = (
            (latest_price_for_views / price_then - 1) * 100
            if price_then and latest_price_for_views is not None else None
        )
        analyst_views.append({
            **row, "latest_price": latest_price_for_views, "pct_change_since": pct_change_since,
        })
    ctx["analyst_views"] = analyst_views

    # 캐싱된 에이전트 분석(있으면). scripts/run_agents.py가 배치로 미리 채워둔
    # 것만 읽는다 — 대시보드를 여는 것 자체는 API를 호출하지 않는다.
    agent_row = store.get_latest_agent_analysis(HYNIX)
    if agent_row:
        ctx["agent_analysis"] = {
            **agent_row,
            "tool_calls": json.loads(agent_row["tool_calls"]) if agent_row.get("tool_calls") else [],
        }

    # 관점별 토론 섀도 기록(scripts/run_debate.py가 쌓은 것) — 읽기 전용 표시만. 위험 점수·상태 계산은 이 값을
    # 전혀 쓰지 않는다(아래 risk_state 계산 경로와 섞지 않음). 대시보드를 열어도 API를 호출하지 않는다.
    debates = store.get_debate_shadow()
    if not debates.empty:
        last = debates.iloc[-1]
        ctx["debate"] = {
            "decision_date": last["decision_date"], "conclusion": last["conclusion"], "model": last["model"],
            "model_served": last["model_served"] if isinstance(last["model_served"], str) else None,
            "evidence": json.loads(last["evidence"]) if last["evidence"] else [],
            "summary": last["synthesis"] if isinstance(last["synthesis"], str) else "",
            "n_records": len(debates), "n_review": len(store.get_debate_review()),
        }
    else:
        ctx["debate"] = {"n_records": 0, "n_review": len(store.get_debate_review())}

    # 등록된 수급 이벤트(자사주 매입·실적발표·콘퍼런스콜) 중 진행 중이거나 앞으로 올 것.
    # 시세 유무와 무관(events는 별도 테이블).
    ctx["watch_events"] = _upcoming_events(store.get_events(HYNIX), today)

    hynix = store.get_prices(HYNIX)
    ctx["has_data"] = not hynix.empty
    if not ctx["has_data"]:
        ctx["recent_logs"] = _recent_logs()
        return ctx

    skhy = store.get_prices(ADR)
    fx = store.get_prices(FX)
    sox = store.get_prices(BENCHMARK)
    vix = store.get_prices("VIX")

    # --- KPI 리본 ---
    ctx["kpi"] = {
        "hynix_close": float(hynix.iloc[-1]),
        "hynix_chg": _pct_change(hynix),
        "skhy_close": float(skhy.iloc[-1]) if not skhy.empty else None,
        "skhy_chg": _pct_change(skhy) if not skhy.empty else None,
    }

    # --- 메인 차트: 000660 vs SKHY(정규화) 90일 ---
    hynix_recent = hynix.tail(CHART_DAYS)
    skhy_recent = _normalize_to(skhy.tail(CHART_DAYS), float(hynix_recent.iloc[0])) if not skhy.empty else skhy
    ctx["main_chart_svg"] = charts.line_chart(
        [hynix_recent, skhy_recent], ["#1d1d1f", "#0071e3"], [False, True],
    )

    # --- ADR 프리미엄 ---
    adr_premium_pct = None
    if not skhy.empty and not fx.empty:
        p = adr.premium(
            krx_close_krw=float(hynix.iloc[-1]),
            adr_close_usd=float(skhy.iloc[-1]),
            usd_krw_rate=float(fx.iloc[-1]),
            ratio=settings.adr_ratio,  # .env의 ADR_RATIO로 override 가능(공식 예탁계약서 미확인, analysis/adr.py 참고)
        )
        adr_premium_pct = p.premium_pct
        ctx["adr"] = p
    ctx["kpi"]["adr_premium_pct"] = adr_premium_pct

    # --- 반도체 관련주 미니차트 + 000660과의 상관관계 ---
    hynix_vs_sox = None
    for name in PEERS:
        s = store.get_prices(name)
        if s.empty:
            continue
        chg = _pct_change(s)
        ctx["peers"].append({
            "ticker": name,
            "chg": chg,
            "spark_svg": charts.sparkline(s.tail(CHART_DAYS), up=(chg or 0) >= 0),
        })

        c = corr.correlation(hynix, s, window=CORR_WINDOW)
        b = corr.beta(hynix, s, window=CORR_WINDOW)
        lags = corr.lagged_correlation(s, hynix, max_lag=3, window=CORR_WINDOW)
        best_lag_n, best_lag_r = max(
            lags.items(),
            key=lambda kv: abs(kv[1].coefficient) if kv[1].coefficient == kv[1].coefficient else -1,
        )
        row = {
            "ticker": name, "coefficient": c.coefficient, "samples": c.samples,
            "reliable": c.reliable, "beta": b, "best_lag": best_lag_n,
            "best_lag_coef": best_lag_r.coefficient,
        }
        ctx["correlations"].append(row)
        if name == BENCHMARK:
            hynix_vs_sox = row

    # --- 최근 급변일 카드: 같은 날 함께 움직인 지표(인과 단정 안 함) ---
    # 지표 시세가 DB에 없으면 그 지표는 "데이터 없음"으로 카드에 남는다(지어내지 않음).
    move_indicators = {spec.key: store.get_prices(spec.key) for spec in mva.DEFAULT_INDICATORS}
    flow_df = store.get_flow(HYNIX, days=100000)  # 급변일 행만 쓰므로 전체를 읽는다
    # items는 카드 날짜를 알아야 조회할 수 있어서 2단계로: 먼저 날짜만 확정한다.
    move_days = [d.date().isoformat() for d in mva.recent_move_dates(hynix, MOVE_THRESHOLD_PCT, max_cards=MOVE_CARDS)]
    items_by_date = store.get_items_on_dates(HYNIX, move_days, stock_code=HYNIX)
    ctx["move_cards"] = mva.build_move_cards(
        hynix, move_indicators, flow=flow_df, items_by_date=items_by_date,
        threshold_pct=MOVE_THRESHOLD_PCT, beta_window=MOVE_BETA_WINDOW, max_cards=MOVE_CARDS,
    )
    ctx["move_meta"] = {
        "threshold_pct": MOVE_THRESHOLD_PCT, "beta_window": MOVE_BETA_WINDOW,
        "min_samples": mva.MIN_SAMPLES,
    }

    # --- 자사주 매입 종료 전후 이벤트 스터디 (이미 종료된 매입만, 측정이지 전망 아님) ---
    ctx["event_study"] = _buyback_event_study(store.get_events(HYNIX, "buyback"), hynix, store.get_foreigner_net(HYNIX))

    # --- 시장 심리 게이지 (VIX 기반) ---
    if not vix.empty:
        g = sentiment.vix_gauge(vix)
        ctx["sentiment"] = g
        ctx["gauge_svg"] = charts.gauge_svg(g.score)

    # --- 뉴스 (출처 있는 것 + confidence 라벨) ---
    news_df = store.get_news(limit=NEWS_LIMIT)
    ctx["news"] = _records(news_df)

    # --- 백테스트: 000660, 네 마녀의 날 D-10~D-1 ---
    range_start = date(today.year - BACKTEST_YEARS_BACK, 1, 1)
    witching_dates = cal.historical_quad_witching(range_start, today)
    res = backtest.event_window_returns(
        hynix, witching_dates, entry_offset=-10, exit_offset=-1,
        label="000660: 네 마녀의 날 D-10~D-1",
    )
    if res.n > 0:
        ctx["backtest"] = {
            "label": res.label, "n": res.n, "win_rate": res.win_rate,
            "mean_return": res.mean_return, "worst": res.worst, "best": res.best,
            "std": res.std, "reliable": res.reliable,
        }

    # --- 기술적 지표 규칙 백테스트 (전체 기간, 000660) ---
    # 매매 신호가 아니라 과거 측정이다 — CLAUDE.md 원칙 2(매매 미구현) 그대로,
    # 여기도 조회+계산만 한다. in-sample/out-of-sample 비교·0.75 threshold
    # 비교는 scripts/backtest_strategies.py가 더 자세히 보여준다(대시보드는
    # 한눈에 보는 요약용으로 전체 기간만).
    hynix_ohlc = store.get_ohlc(HYNIX)  # has_data 체크를 이미 통과해서 비어있지 않음
    close_full = hynix_ohlc["close"].dropna().sort_index()
    if len(close_full) >= RULE_BACKTEST_MIN_ROWS:
        open_full = hynix_ohlc["open"].reindex(close_full.index)
        rows = []
        for name, fn in RULE_STRATEGIES:
            holding = fn(close_full)
            r = rbt.backtest_rule(close_full, holding, open_full, label=name)["full"]
            rows.append({
                "name": name,
                "total_return_pct": r.total_return_pct if r.total_return_pct == r.total_return_pct else None,
                "mdd_pct": r.mdd_pct if r.mdd_pct == r.mdd_pct else None,
                "n": r.n,
                "win_rate_pct": r.win_rate if r.win_rate == r.win_rate else None,
                "exposure_pct": r.exposure_pct if r.exposure_pct == r.exposure_pct else None,
                "reliable": r.reliable,
            })
        ctx["rule_backtest"] = {
            "rows": rows,
            "start": close_full.index[0].date().isoformat(),
            "end": close_full.index[-1].date().isoformat(),
            "min_samples": rbt.MIN_SAMPLES,
        }

    # --- 매수/목표/손절 기준가 (백테스트 통과 규칙만, 000660) ---
    # CLAUDE.md 원칙 1(갱신판): 백테스트로 검증된 규칙만 가격을 표시한다.
    # 통과 기준(analysis/price_rule_backtest.passes): 거래 100건 이상, 전체
    # 기간·최근 1년(out-of-sample) 거래당 평균수익 둘 다 양수. 통과 못 한
    # 규칙은 여기서 걸러지고 절대 가격으로 화면에 노출되지 않는다 — 통과한
    # 규칙이 하나도 없으면 "오늘 해당 규칙 없음"으로만 표시(아래 템플릿).
    hynix_ohlc_recent = hynix_ohlc.loc[hynix_ohlc.index >= pd.Timestamp(PRICE_RULE_START)]
    if len(hynix_ohlc_recent) >= RULE_BACKTEST_MIN_ROWS:
        passing_price_rules = []
        for name, spec in price_rules.RULES.items():
            levels = spec["fn"](hynix_ohlc_recent)
            results = prb.backtest_price_rule(hynix_ohlc_recent, levels, entry_type=spec["entry_type"], label=spec["label"])
            if not prb.passes(results):
                continue
            full = results["full"]
            oos = results["out_of_sample"]
            entry, target, stop = (
                levels["entry_price"].iloc[-1],
                levels["target_price"].iloc[-1],
                levels["stop_price"].iloc[-1],
            )
            passing_price_rules.append({
                "name": name, "label": spec["label"],
                "has_signal_today": entry == entry,  # NaN이면 오늘은 신호 없음
                "entry_price": float(entry) if entry == entry else None,
                "target_price": float(target) if target == target else None,
                "stop_price": float(stop) if stop == stop else None,
                "n": full.n, "win_rate_pct": full.win_rate, "mean_return_pct": full.mean_return,
                "mdd_pct": full.mdd_pct, "reliable": full.reliable,
                "oos_n": oos.n, "oos_mean_return_pct": oos.mean_return,
            })
        ctx["price_rule_signals"] = passing_price_rules

    # --- 전문가 경험칙 신호: 위험 점수·오늘의 상태·전문가별 가중치 ---
    # analysis/signals.py가 계산한 신호(scripts/compute_signals.py가
    # signal_values에 채움)와 analysis/weighting.py가 매주 재계산한 가중치
    # (scripts/recompute_weights.py가 weight_history에 기록)를 조합한다.
    # 이 대시보드는 그 결과를 읽기만 한다 — 주문은 없다(원칙 2).
    signals_df = store.get_signals(enabled_only=True)
    current_weights = store.get_current_weights()
    expert_perf: dict[str, list[tuple[float, int]]] = {}  # expert_id -> [(hit_rate_pct, n), ...]
    combo_value = None
    full_perf_by_sid: dict[str, sb.SignalPerformance] = {}
    oos_perf_by_sid: dict[str, sb.SignalPerformance] = {}
    on_sid_meta: dict[str, dict] = {}  # q_value를 뺀 나머지 — BH 보정 뒤에 채워 넣는다

    for _, srow in signals_df.iterrows():
        sid, direction = srow["signal_id"], srow["direction"]
        values = store.get_signal_values(sid)
        if values.empty:
            continue
        latest = values.sort_values("date").iloc[-1]
        if sid == "KHJ_COMBO":
            combo_value = latest["value"]
        if direction not in ("risk", "opportunity"):
            continue

        # 신호별 과거 성적은 결정일 정렬 규칙으로 평가한다(analysis/signal_alignment.py).
        # 반면 "지금 켜진 신호"(latest)는 정렬하지 않는다 — 대시보드를 보는 시점에는 해외
        # 세션의 가장 최근 종가도 이미 나와 있는 정보라서 미래참조가 아니다.
        is_on_series = sa.aligned_is_on(sid, values, hynix.sort_index().index)
        perf = sb.evaluate_signal(is_on_series, hynix, direction=direction, horizons=(sb.PRIMARY_HORIZON,))
        full = perf[sb.PRIMARY_HORIZON]["full"]
        full_perf_by_sid[sid] = full
        oos_perf_by_sid[sid] = perf[sb.PRIMARY_HORIZON]["out_of_sample"]
        expert_perf.setdefault(srow["expert_id"], []).append((full.hit_rate_pct, full.n))

        if bool(latest["is_on"]):
            on_sid_meta[sid] = {
                "direction": direction, "weight_pct": current_weights.get(sid, 0.0),
                "name": srow["name"], "expert_id": srow["expert_id"], "source_ref": srow["source_ref"],
                "approximate": sid in APPROXIMATE_SIGNALS,
            }

    # 다중비교 보정: 이 화면에 떠 있는 신호 전체(20거래일·전체 기간)를 한
    # 묶음으로 본다(scripts/signal_scorecard.py와 같은 원칙) — q_value는
    # full_perf_by_sid의 객체를 제자리 수정하므로, 그 다음에 on_signals를
    # 조립해야 보정된 값이 반영된다.
    sb.apply_benjamini_hochberg(list(full_perf_by_sid.values()))

    on_signals: dict[str, dict] = {}
    for sid, meta in on_sid_meta.items():
        full, oos = full_perf_by_sid[sid], oos_perf_by_sid[sid]
        on_signals[sid] = {
            **meta, "n": full.n, "hit_rate_pct": full.hit_rate_pct, "reliable": full.reliable,
            "n_events": full.n_events, "ci_lower_pct": full.ci_lower_pct,
            "ci_upper_pct": full.ci_upper_pct, "base_rate_pct": full.base_rate_pct,
            "significant": full.significant, "q_value": full.q_value,
            "oos_n_events": oos.n_events, "oos_hit_rate_pct": oos.hit_rate_pct_events,
        }

    risk_state = rs.build_risk_state(on_signals, combo_value=combo_value)
    ctx["group_state"] = _group_state(signals_df, hynix)

    # 점수 상한 경고: 값이 없는 risk 신호(주로 수동 입력 누락)는 켜질 수 없으므로 도달 못 하는 단계가 생긴다.
    # "값이 있음" = 마지막 000660 거래일 기준 최근 SIGNAL_RECENT_DAYS 안에 signal_values 행이 있음.
    hynix_last = hynix.index.max()
    available: set[str] = set()
    for _, srow in signals_df.iterrows():
        v = store.get_signal_values(srow["signal_id"])
        if not v.empty and v["date"].max() >= hynix_last - pd.Timedelta(days=SIGNAL_RECENT_DAYS):
            available.add(srow["signal_id"])
    directional = signals_df[signals_df["direction"].isin(["risk", "opportunity"])]
    ceiling = rs.attainable_ceiling(
        {sid: current_weights.get(sid, 0.0) for sid in directional["signal_id"]},
        dict(zip(directional["signal_id"], directional["direction"])), available,
    )
    manual_ids = set(signals_df.loc[signals_df["is_manual"] == 1, "signal_id"]) if "is_manual" in signals_df else set()
    blocked = [u for u in ceiling["unreachable"] if not u["structural"]]
    if blocked:
        missing = sorted({sid for u in blocked for sid in u["missing"]})
        ctx["score_ceiling"] = {
            "max_score": ceiling["max_score"],
            "stages": "·".join(u["stage"].replace("단계", "") for u in blocked) + "단계",
            "missing": [f"{sid}(수동 입력 없음)" if sid in manual_ids else f"{sid}(값 없음)" for sid in missing],
            "structural": [u["stage"] for u in ceiling["unreachable"] if u["structural"]],
        }

    ctx["risk_state"] = {
        "risk_score": risk_state.risk_score,
        "opportunity_score": risk_state.opportunity_score,
        "risk_weight_sum": risk_state.risk_weight_sum,
        "opportunity_weight_sum": risk_state.opportunity_weight_sum,
        "combo_triggered": risk_state.combo_triggered,
        "new_buy_status": risk_state.new_buy_status,
        "sell_stage_if_holding": risk_state.sell_stage_if_holding,
        "on_signals": [
            {**on_signals[row["signal_id"]], "signal_id": row["signal_id"]}
            for row in risk_state.on_signals
        ],
        # 5단계: 점수 분해(신호별 기여도 바)·쏠림 경고. contributions는
        # on_signals와 별개 리스트라, 템플릿에서 이름/전문가를 같이 보여주려면
        # on_signals 메타를 붙여야 한다 — 여기서 미리 합쳐둔다.
        "contributions": [
            {**c, **on_signals[c["signal_id"]]} for c in risk_state.contributions
        ],
        "concentration_warnings": risk_state.concentration_warnings,
    }

    # 5단계: 수동 입력 최신성 — 수동입력 의존 신호(KHJ_RATE_STAGE·
    # YJH_MEMORY_MARGIN·YJH_PBR_FLOOR)가 쓰는 입력이 오래되지 않았는지
    # 대시보드에 "최신성" 배지로 보여준다. 점수 계산에서 자동으로 빼지는
    # 않는다(기본값 — 사용자가 아직 "점수에서 뺄지"를 정하지 않았으므로
    # 관찰만 하고 멈춘다, 5단계 요청의 "포함 여부는 옵션"이라는 문구 그대로
    # "항상 포함 + 경고만 표시"를 기본 옵션으로 선택).
    ctx["manual_input_freshness"] = {
        "rate_stage": store.manual_input_freshness("rate_stage"),
        "memory_margin": store.manual_input_freshness("memory_margin"),
        "pbr": store.manual_input_freshness("pbr"),
    }

    expert_weights = wt.expert_weight_totals(current_weights, dict(zip(signals_df["signal_id"], signals_df["expert_id"])))
    experts_df = store.get_experts()
    for _, erow in experts_df.iterrows():
        eid = erow["expert_id"]
        perfs = expert_perf.get(eid, [])
        reliable_perfs = [(hr, n) for hr, n in perfs if n > 0]
        avg_hit_rate = (sum(hr for hr, _ in reliable_perfs) / len(reliable_perfs)) if reliable_perfs else None
        total_n = sum(n for _, n in perfs)
        ctx["expert_rows"].append({
            "expert_id": eid, "name": erow["name"],
            "title": erow["title"] if pd.notna(erow["title"]) else None,  # NULL이 NaN으로 읽혀 "(nan)"으로 찍히던 버그
            "n_signals": len(perfs), "avg_hit_rate_pct": avg_hit_rate,
            "total_n": total_n, "weight_pct": expert_weights.get(eid, 0.0),
        })

    # --- 프레임워크 렌즈 ---
    has_period_event = any(e.kind == "period" for e in ctx["today_events"])
    earnings_days = next(
        (e.date for e in cal.upcoming(today, 14) if e.kind == "earnings"), None
    )
    earnings_within = (earnings_days - today).days if earnings_days else None

    ctx["lenses"] = [
        frameworks.risk_lens(
            backtest_n=res.n, backtest_reliable=res.reliable,
            backtest_worst_pct=res.worst if res.n else None,
            adr_premium_pct=adr_premium_pct,
        ),
        frameworks.cycle_lens(
            has_active_period_event=has_period_event,
            upcoming_earnings_within_days=earnings_within,
            beta_vs_benchmark=hynix_vs_sox["beta"] if hynix_vs_sox else None,
            correlation_reliable=hynix_vs_sox["reliable"] if hynix_vs_sox else False,
        ),
        frameworks.fact_checker_lens(cited=[
            {"label": "000660 vs SOX 상관계수",
             "value": hynix_vs_sox["coefficient"] if hynix_vs_sox else None,
             "samples": hynix_vs_sox["samples"] if hynix_vs_sox else None},
            {"label": "네 마녀의 날 백테스트 최대손실",
             "value": res.worst if res.n else None, "samples": res.n},
            {"label": "ADR 프리미엄", "value": adr_premium_pct, "samples": None},
        ]),
        frameworks.VALUE_LENS_STUB(),
        frameworks.GROWTH_LENS_STUB(),
        frameworks.VALUATION_LENS_STUB(),
    ]

    ctx["recent_logs"] = _recent_logs()
    return ctx


def _upcoming_events(events_df, today: date) -> list[dict]:
    """종료일이 오늘 이후인(진행 중이거나 예정된) 이벤트를 가까운 순으로. 상태 문구는 일정 사실만 말한다."""
    if events_df is None or events_df.empty:
        return []
    out = []
    for r in _records(events_df):
        start, end = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
        if end < today:
            continue
        if start <= today:
            status = f"진행 중 · 종료까지 {(end - today).days}일"
        else:
            status = f"D-{(start - today).days}"
        out.append({
            **r, "label": ev.EVENT_TYPES.get(r["event_type"], r["event_type"]),
            "period": r["start_date"] if start == end else f"{r['start_date']} ~ {r['end_date']}",
            "status": status, "_sort": start,
        })
    out.sort(key=lambda e: e["_sort"])
    return out


def _buyback_event_study(buybacks_df, close, foreigner) -> dict:
    """
    종료된 자사주 매입(end_date가 마지막 시세일 이하)의 종료일 전후 N거래일 수익률·외국인 수급.
    이벤트가 없으면 데이터 없음 상태(n_events=0)로 반환 — 템플릿이 "데이터 없음"을 보여준다.
    """
    end_dates = [] if buybacks_df is None or buybacks_df.empty else list(buybacks_df["end_date"])
    windows = []
    for n in EVENT_STUDY_WINDOWS:
        windows.append(evs.event_study(close, end_dates, n_days=n, foreigner=foreigner))
    has_flow = foreigner is not None and not foreigner.dropna().empty
    return {
        "n_events": len(end_dates), "windows": windows, "has_flow": has_flow,
        "min_samples": evs.MIN_SAMPLES,
    }


def _recent_logs() -> list[dict]:
    logs = store.recent_logs(5)
    return _records(logs)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    context = build_dashboard_context()
    # Starlette 신버전(>=0.29)은 TemplateResponse(request, name, context) 시그니처다.
    # 옛 (name, context) 시그니처는 Starlette 1.x에서 TypeError(unhashable dict)로 죽는다.
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/healthz")
def healthz() -> dict:
    """배치/모니터링용. 예측이나 시세 정보는 담지 않는다."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.main:app", host="127.0.0.1", port=8000, reload=True)
