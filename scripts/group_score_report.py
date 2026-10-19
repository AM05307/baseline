"""
신호 그룹 방식(기본) vs 적중률 가중치 방식(옵션) 비교 — 기준 기간과 검증 기간을 따로. DB 읽기만.

    python scripts/group_score_report.py               # 000660
    python scripts/group_score_report.py --ticker 000660 --horizons 5,20,63

기준점은 기준 기간(2005-01-01~2018-12-31) 분포에서 한 번 정한 고정값이다. 검증 기간(2019-01-01~최신) 결과는 보고만 하고
기준점을 바꾸지 않는다(analysis/group_score 모듈 docstring).

출력:
  1. 기준점(요청 비율 vs 동점 때문에 실제 비율)
  2. 단계별 날짜 비율 — 방식 × 구간
  3. 단계별 이후 N거래일 수익률(t일 종가 → t+N일 종가): n · 평균 · 중앙값 · 최악(원칙 3·4). 겹치는 날 표본이라 비겹침 n ≈ n/N
  4. 단계 비중(100/66/33/0%)으로 운용했을 때 vs 단순 보유: 누적·MDD·비중 변경 횟수(t일 종가 결정 → t+1일 시가 체결, 비용 반영)
과거 데이터에 대한 측정이며 향후 수익을 보장하지 않는다. 매수·매도 추천이 아니다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import group_score as gs  # noqa: E402
from analysis import risk_score as rs  # noqa: E402
from analysis import trading_costs as tc  # noqa: E402
from analysis import what_if as wi  # noqa: E402
from db import store  # noqa: E402
from scripts import what_if_recent as wir  # noqa: E402

PERIODS = {"기준 기간": (gs.CALIBRATION_START, gs.CALIBRATION_END), "검증 기간": (gs.VALIDATION_START, None)}
MIN_SAMPLES = 30


def stage_frames(ticker: str) -> tuple[pd.Series, pd.Series, pd.DataFrame, pd.DataFrame]:
    close, ids, dirs, values, wh = wir.load_inputs(ticker)
    opens = wir.load_opens(ticker)
    sw = wi.DEFAULT_STAGE_WEIGHTS
    grp, _ = wir.build_model_frame(close, ids, dirs, values, wh, sw, wi.DEFAULT_COMBO_STAGE, rs.DEFAULT_RISK_THRESHOLD,
                                   rs.DEFAULT_SELL_STAGE_2_THRESHOLD, method="group", calibration_dates=wir.kr_calendar())
    wtd, _ = wir.build_model_frame(close, ids, dirs, values, wh, sw, wi.DEFAULT_COMBO_STAGE, rs.DEFAULT_RISK_THRESHOLD,
                                   rs.DEFAULT_SELL_STAGE_2_THRESHOLD, method="weighted")
    return close, opens, grp, wtd


def _slice(s: pd.Series | pd.DataFrame, start: str, end: str | None):
    m = s.index >= pd.Timestamp(start)
    if end:
        m &= s.index <= pd.Timestamp(end)
    return s[m]


def forward_stats(close: pd.Series, stage: pd.Series, horizon: int) -> dict[int, dict]:
    fwd = close.shift(-horizon) / close - 1
    out = {}
    for k in range(4):
        r = fwd[(stage == k) & fwd.notna()]
        out[k] = {"n": int(len(r)), "mean": float(r.mean()) if len(r) else np.nan,
                  "median": float(r.median()) if len(r) else np.nan, "worst": float(r.min()) if len(r) else np.nan}
    return out


def strategy_result(close: pd.Series, opens: pd.Series, decided: pd.Series, start: str, end: str | None) -> dict:
    idx = _slice(close, start, end).index
    pos = close.index.get_loc(idx[0])
    idx = close.index[max(0, pos - 1):close.index.get_loc(idx[-1]) + 1]  # 첫날 비중을 정할 직전 거래일 포함
    costs = wi.CostModel(tc.DEFAULT_COMMISSION_RATE, 0.0, tc.tax_rate_on)
    o = opens.reindex(idx).fillna(close.reindex(idx).shift(1)).fillna(close.reindex(idx))  # 시가 없는 날은 전일 종가(갭 0)
    a = wi.simulate_next_open(close[idx], o, decided.reindex(idx).fillna(1.0), 1.0, costs)
    b = wi.simulate_next_open(close[idx], o, pd.Series(1.0, index=idx), 1.0, costs)
    return {"a": a["equity"].iloc[-1] - 1, "b": b["equity"].iloc[-1] - 1, "a_mdd": wi.max_drawdown(a["equity"]),
            "b_mdd": wi.max_drawdown(b["equity"]), "changes": int(a["changed"].sum()), "days": len(idx) - 1,
            "missing_open": int(opens.reindex(idx).isna().sum())}


def run(ticker: str = "000660", horizons=(5, 20, 63)) -> int:
    store.init_db()
    close, opens, grp, wtd = stage_frames(ticker)
    if close.empty:
        print(f"[{ticker}] 시세 없음")
        return 1
    thr = grp.attrs["thresholds"]
    print(f"[{ticker}] 마지막 거래일 {close.index[-1].date()}")
    print("\n=== 1. 그룹 방식 기준점(기준 기간 " f"{thr.start}~{thr.end}, n={thr.n_days}일, 한국 거래일) ===")
    for k, (q, v, a) in enumerate(zip(thr.quantiles, thr.values, thr.actual_shares), start=1):
        print(f"  {k}단계: 상위 {q:.0%} 요청 → 위험 점수 {('도달 불가' if v == float('inf') else f'{v:.1f}')} 이상 "
              f"(기준 기간 실제 {a:.1%})")
    print("  예전 방식: 위험 점수 60점 이상 1단계, 80점 이상 2단계, KHJ_COMBO==3이면 3단계(적중률 가중치, weight_history as-of)")

    methods = {"그룹(기본)": grp, "적중률 가중치(옵션)": wtd}
    print("\n=== 2. 단계별 날짜 비율 ===")
    print(f"{'방식':18s} {'구간':8s} {'일수':>6s} " + " ".join(f"{wi.STAGE_LABELS[k]:>7s}" for k in range(4)))
    for mname, st in methods.items():
        for pname, (a, b) in PERIODS.items():
            s = _slice(st, a, b)
            s = s[s["has_weights"]] if "has_weights" in s else s
            shares = [float((s["stage"] == k).mean()) * 100 if len(s) else np.nan for k in range(4)]
            print(f"{mname:18s} {pname:8s} {len(s):>6d} " + " ".join(f"{x:>6.1f}%" for x in shares))
    print("  (적중률 가중치 방식은 weight_history가 없는 날(냉시작)을 뺐다)")

    print("\n=== 3. 단계별 이후 N거래일 수익률(t일 종가 → t+N일 종가) ===")
    for h in horizons:
        print(f"\n[{h}거래일 뒤]  표본은 날짜 단위로 서로 겹침(비겹침 n ≈ n/{h}). n<{MIN_SAMPLES} ⚠")
        print(f"{'방식':18s} {'구간':8s} {'단계':6s} {'n':>6s} {'평균':>8s} {'중앙값':>8s} {'최악':>8s}")
        for mname, st in methods.items():
            for pname, (a, b) in PERIODS.items():
                s = _slice(st, a, b)
                s = s[s["has_weights"]] if "has_weights" in s else s
                fs = forward_stats(close.reindex(close.index), s["stage"].reindex(close.index).fillna(-1), h)
                for k in range(4):
                    f = fs[k]
                    warn = " ⚠" if f["n"] < MIN_SAMPLES else ""
                    vals = "데이터 없음" if f["n"] == 0 else (f"{f['mean']*100:>+7.2f}% {f['median']*100:>+7.2f}% "
                                                         f"{f['worst']*100:>+7.2f}%")
                    print(f"{mname:18s} {pname:8s} {wi.STAGE_LABELS[k]:6s} {f['n']:>6d} {vals}{warn}")

    print("\n=== 4. 단계 비중(100/66/33/0%) 운용 vs 단순 보유 (t 종가 결정 → t+1 시가 체결, 수수료·매도세 반영) ===")
    print(f"{'방식':18s} {'구간':8s} {'거래일':>6s} {'모델 누적':>10s} {'모델 MDD':>9s} {'보유 누적':>10s} {'보유 MDD':>9s} {'변경':>5s}")
    for mname, st in methods.items():
        for pname, (a, b) in PERIODS.items():
            decided = st["weight_next"].where(st["has_weights"], 1.0)
            r = strategy_result(close, opens, decided, a, b)
            mark = "  ← 단순 보유보다 낮음" if r["a"] < r["b"] else ""
            print(f"{mname:18s} {pname:8s} {r['days']:>6d} {r['a']*100:>+9.2f}% {r['a_mdd']*100:>+8.2f}% "
                  f"{r['b']*100:>+9.2f}% {r['b_mdd']*100:>+8.2f}% {r['changes']:>5d}{mark}")
    print("  (적중률 가중치 방식의 냉시작 날은 100% 보유로 둠. 시가가 없는 날은 전일 종가 = 갭 0으로 대신함)")
    print("\n과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. 이 결과는 매수·매도 추천이 아닙니다.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="그룹 방식 vs 적중률 가중치 방식 비교(기준/검증 기간)")
    p.add_argument("--ticker", default="000660")
    p.add_argument("--horizons", default="5,20,63")
    a = p.parse_args(argv)
    return run(a.ticker, tuple(int(x) for x in a.horizons.split(",")))


if __name__ == "__main__":
    sys.exit(main())
