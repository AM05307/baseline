"""
"자금 배분 현황" 화면 — executor 전용 읽기 전용 페이지(GET만, DB에 쓰지 않음).

baseline 대시보드(web/main.py)에 카드로 넣지 않은 이유: baseline 쪽에서 executor를 import하면 의존 방향
규칙(executor → baseline 한 방향)이 깨진다. 그래서 같은 내용을 executor가 자기 페이지로 보여준다.

실행:
    uvicorn executor.web:app --port 8001
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from executor import config_files, metrics, store
from executor.criteria import promotion_status
from executor.tiers import Ladder

NO_DATA = "데이터 없음"


def _won(v) -> str:
    return NO_DATA if v is None or v != v else f"{v:,.0f}원"


def _pct(v) -> str:
    return NO_DATA if v is None or v != v else f"{v:+.2f}%"


def _num(v) -> str:
    if v is None or v != v:
        return NO_DATA
    if v == float("inf"):
        return "∞"
    return f"{v:,.2f}" if not float(v).is_integer() else f"{v:,.0f}"


def build_allocation_context(ladder: Ladder, criteria: dict, benchmark_close: pd.Series | None = None,
                             path: Path | None = None) -> dict:
    hist = store.tier_history(path)
    ctx: dict = {"started": not hist.empty, "stints": [], "demotions": [], "criteria": None}

    contrib = store.contributions(path)
    val = store.latest_savings_valuation(path)
    to_sav = contrib.loc[contrib["destination"] == "savings", "amount_krw"] if not contrib.empty else pd.Series(dtype=float)
    to_trd = contrib.loc[contrib["destination"] == "trading", "amount_krw"] if not contrib.empty else pd.Series(dtype=float)
    ctx["savings"] = {
        "contributed": _won(float(to_sav.sum())) if len(to_sav) else NO_DATA, "n": int(len(to_sav)),
        "trading_contributed": _won(float(to_trd.sum())) if len(to_trd) else NO_DATA, "n_trading": int(len(to_trd)),
        "valuation": _won(val["value_krw"]) if val else NO_DATA, "valuation_as_of": val["as_of"] if val else NO_DATA,
    }
    if hist.empty:
        return ctx

    cur = hist.iloc[-1]
    tier = ladder.get(cur["to_tier"])
    snaps = store.snapshots(tier.account, path=path)
    last = snaps.iloc[-1] if not snaps.empty else None
    ctx["current"] = {
        "tier": tier.name, "kind": "실제 자금" if tier.kind == "real" else "가상 자금",
        "cap": _won(tier.max_capital_krw) if tier.max_capital_krw is not None else "미확정",
        "entered": cur["effective_on"],
        "account_value": _won(last["account_value"]) if last is not None else NO_DATA,
        "operating_capital": _won(last["operating_capital"]) if last is not None else NO_DATA,
        "outside_cash": _won(last["outside_cash"]) if last is not None else NO_DATA,
        "sleeve_value": _won(last["sleeve_value"]) if last is not None else NO_DATA,
        "as_of": last["date"] if last is not None else NO_DATA,
        "halted": store.is_halted(tier.account, path),
        "freeze": store.freeze_state(tier.account, path),
    }

    rows = hist.to_dict("records")
    for i, r in enumerate(rows):
        end = rows[i + 1]["effective_on"] if i + 1 < len(rows) else None
        t = ladder.get(r["to_tier"])
        m = metrics.tier_metrics_from_db(t.account, r["effective_on"], end, benchmark_close, path)
        ctx["stints"].append({
            "tier": t.name, "entered": r["effective_on"], "left": end or "진행 중", "event": r["event"],
            "days": m.days, "trades": m.trades, "cum": _pct(m.cum_return_pct), "mdd": _pct(m.mdd_pct),
            "bh_cum": _pct(m.bh_return_pct), "bh_mdd": _pct(m.bh_mdd_pct),
            "benchmarks": [{"name": k, "cum": _pct(v["return_pct"]), "mdd": _pct(v["mdd_pct"])}
                           for k, v in m.benchmarks.items()],
        })
        if r["event"] == "demote":
            ctx["demotions"].append({"at": r["effective_on"], "from": r["from_tier"], "to": r["to_tier"],
                                     "reason": r["reason"] or ""})
    ctx["demotions"] = ctx["demotions"][-5:][::-1]

    m_now = metrics.tier_metrics_from_db(tier.account, cur["effective_on"], None, benchmark_close, path)
    st = promotion_status(ladder, criteria, tier.name, m_now)
    ctx["criteria"] = {
        "next_tier": st.next_tier or "없음", "stage": st.stage or "없음",
        "confirmed": st.criteria_confirmed, "eligible": st.eligible, "blockers": st.blockers,
        "days": m_now.days, "trades": m_now.trades,
        "items": [{"label": i.label, "passed": i.passed, "actual": _num(i.actual), "threshold": _num(i.threshold),
                   "note": i.note} for i in st.items],
    }
    return ctx


def calendar_warning(today: date | None = None) -> str | None:
    """다음 해 휴장일 목록이 없고 12/1 이후면 경고 문구(읽기만, DB에 쓰지 않음)."""
    from executor.krx_calendar import next_year_warning
    return next_year_warning(config_files.load_calendar(record=False), today or date.today())


try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse
    from fastapi.templating import Jinja2Templates
except ImportError:  # fastapi 미설치 환경에서도 build_allocation_context는 쓸 수 있게
    FastAPI = None


def _make_app():
    app = FastAPI(title="executor 자금 배분 현황")
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        warn = calendar_warning()
        ladder = config_files.load_ladder(record=False)
        criteria = config_files.load_criteria(record=False)
        cfg = config_files.load_executor_config(record=False)
        if store.DB_PATH.exists():
            ctx = build_allocation_context(ladder, criteria, metrics.baseline_benchmarks(cfg["benchmarks"]))
        else:
            ctx = {"started": False, "savings": None, "stints": [], "demotions": [], "criteria": None}
        ctx["calendar_warning"] = warn
        tiers_file = config_files.resolve_config("capital_tiers")
        ctx["example_config"] = tiers_file.name if config_files.is_example(tiers_file) else None
        return templates.TemplateResponse(request, "allocation.html", ctx)

    return app


app = _make_app() if FastAPI is not None else None
