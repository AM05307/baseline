"""
관점별 토론 섀도 기록 채점 — 기록이 앞으로 쌓인 뒤에만 의미가 있다(과거 날짜 토론은 만들지 않는다: agents/debate.py).

  1. 종합 결론(위험 높음/중립/위험 낮음)별로, 기준일 종가 대비 이후 5/20/63거래일 000660 수익률의 n·평균·중앙값·최대손실.
  2. 같은 기준일의 위험 점수 상태(baseline 모델, 분리 방식)와의 일치율 — 대응 규칙은 config/debate.yaml
     scorecard.risk_state_map(관례이며 1:1 대응이 아니다). 교차표도 함께 보여준다.
표본수와 최대손실은 항상 같이 표시하고, 표본수가 min_samples(기본 60) 미만이면 "판단 불가"라고 쓴다.
DB 읽기만 한다. 토론 결과를 어디에도 반영하지 않는다.

사용:
    python scripts/debate_scorecard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import signal_alignment as sa  # noqa: E402
from analysis import signal_backtest as sb  # noqa: E402
from analysis import what_if as wi  # noqa: E402
from db import store  # noqa: E402
from scripts.run_debate import load_config  # noqa: E402

HYNIX = "000660"
STATE_LABELS = {0: "정상", 1: "1단계", 2: "2단계 이상", 3: "2단계 이상"}


def risk_states_on(dates: list[str], method: str = wi.DEFAULT_SCORE_METHOD) -> dict[str, str]:
    """기준일마다 baseline 위험 점수 상태. 계산 경로는 scripts/what_if_recent.py와 같다(기본 그룹 방식, 옵션 weighted)."""
    if not dates:
        return {}
    sig = store.get_signals(enabled_only=True)
    sig = sig[sig["direction"].isin(["risk", "opportunity"])]
    ids = list(sig["signal_id"])
    if not ids:
        return {}
    idx = pd.DatetimeIndex(sorted(set(pd.to_datetime(dates))))
    if method == "group":
        grid = idx.union(store.get_prices("KOSPI").index)
        is_on_all, _ = sa.align_signals({s: store.get_signal_values(s) for s in ids}, grid)
        try:
            st = wi.group_states(is_on_all, idx)
        except ValueError:  # 기준 기간 데이터 없음 — 상태를 지어내지 않고 빈 결과(채점표에 "데이터 없음")
            return {}
        return {d.date().isoformat(): STATE_LABELS[int(s)] for d, s in st["stage"].items()}
    is_on, value = sa.align_signals({s: store.get_signal_values(s) for s in ids}, idx)
    weights = wi.weights_for_dates(store.get_weight_history(), idx, ids)
    combo = value["KHJ_COMBO"] if "KHJ_COMBO" in value.columns else None
    st = wi.score_and_stage(is_on, weights, dict(zip(sig["signal_id"], sig["direction"])), combo)
    has_w = weights.notna().any(axis=1)
    return {d.date().isoformat(): STATE_LABELS[int(s)] for d, s in st["stage"].items() if has_w[d]}


def compute_scorecard(rows: pd.DataFrame, close: pd.Series, risk_by_date: dict[str, str],
                      horizons=(5, 20, 63), min_samples: int = 60, risk_map: dict | None = None) -> dict:
    close = close.sort_index()
    out = {"returns": [], "agreement": None, "n_records": len(rows)}
    if rows.empty:
        return out
    dates = pd.to_datetime(rows["decision_date"])
    for h in horizons:
        fwd = sb.forward_returns(close, h)
        vals = fwd.reindex(dates).to_numpy()
        df = pd.DataFrame({"conclusion": rows["conclusion"].to_numpy(), "ret": vals}).dropna()
        for c in ("위험 높음", "중립", "위험 낮음"):
            r = df.loc[df["conclusion"] == c, "ret"]
            n = len(r)
            out["returns"].append({
                "conclusion": c, "horizon": h, "n": n,
                "mean": float(r.mean()) if n else None, "median": float(r.median()) if n else None,
                "worst": float(r.min()) if n else None,
                "verdict": "판단 불가" if n < min_samples else "표본 충분",
            })
    risk_map = risk_map or {}
    pairs = [(c, risk_by_date.get(d)) for c, d in zip(rows["conclusion"], rows["decision_date"]) if risk_by_date.get(d)]
    if pairs:
        matches = sum(1 for c, s in pairs if risk_map.get(s) == c)
        table = pd.crosstab(pd.Series([c for c, _ in pairs], name="토론 결론"), pd.Series([s for _, s in pairs], name="위험 상태"))
        out["agreement"] = {"n": len(pairs), "matches": matches, "rate": matches / len(pairs),
                            "verdict": "판단 불가" if len(pairs) < min_samples else "표본 충분", "table": table}
    return out


def _f(x):
    return "-" if x is None else f"{x:+.2f}%"


def main() -> int:
    store.init_db()
    cfg = load_config().get("scorecard", {})
    rows = store.get_debate_shadow()
    min_n = int(cfg.get("min_samples", 60))
    print(f"[debate_scorecard] 섀도 기록 {len(rows)}건 · 판단 기준 표본 {min_n}건 "
          f"(검토 대기 {len(store.get_debate_review())}건은 채점에서 제외)")
    if rows.empty:
        print("아직 기록이 없습니다 — scripts/run_debate.py --execute로 앞으로 쌓아야 채점할 수 있습니다.")
        return 0
    sc = compute_scorecard(rows, store.get_prices(HYNIX), risk_states_on(list(rows["decision_date"])),
                           tuple(cfg.get("horizons", (5, 20, 63))), min_n, cfg.get("risk_state_map", {}))
    print(f"\n{'결론':8s} {'기간':>5s} {'n':>5s} {'평균':>9s} {'중앙값':>9s} {'최대손실':>9s}  판정")
    for r in sc["returns"]:
        print(f"{r['conclusion']:8s} {r['horizon']:>4d}일 {r['n']:>5d} {_f(r['mean']):>9s} {_f(r['median']):>9s} "
              f"{_f(r['worst']):>9s}  {r['verdict']}")
    a = sc["agreement"]
    if a:
        print(f"\n같은 날 위험 점수 상태와의 일치율: {a['matches']}/{a['n']} = {a['rate']:.0%} ({a['verdict']}) "
              f"— 대응 규칙 {cfg.get('risk_state_map')}")
        print(a["table"].to_string())
    else:
        print("\n같은 날 위험 점수 상태: 계산할 수 없음(가중치 기록 없음 등)")
    print("\n섀도 기록에 대한 측정이며 향후 수익을 보장하지 않습니다. 매매 결정·위험 점수에 반영하지 않습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
