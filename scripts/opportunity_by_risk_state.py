"""
기회(opportunity) 신호가 켜진 날 이후 5/20/63거래일 수익률을 **그 날의 위험 상태별로** 나눠
보여준다 — "급락 중에 켜지는 기회 신호가 실제로 반등을 맞췄는가"를 확인하는 리포트.

DB 읽기만 한다(주문 없음, 쓰기 없음). 위험 상태는 scripts/what_if_recent.py와 같은 경로로
만든다: 결정일 정렬(analysis/signal_alignment.py) + 그 날짜 이전 weight_history 가중치 +
분리 방식 위험 점수(opportunity는 위험 점수에 안 들어감). 통계는 이벤트 단위(연속으로 켜진
구간의 첫날)로 내고, 같은 상태의 모든 날 평균을 기준으로 나란히 둔다.

사용:
    python scripts/opportunity_by_risk_state.py
    python scripts/opportunity_by_risk_state.py --start 2005-01-01 --horizons 5,20,63
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import risk_score_history as rsh  # noqa: E402
from analysis import signal_backtest as sb  # noqa: E402
from analysis import what_if as wi  # noqa: E402
from db import store  # noqa: E402
from scripts import what_if_recent as wir  # noqa: E402

STATE_LABELS = {0: "정상", 1: "1단계", 2: "2단계 이상", 3: "2단계 이상"}
STATE_ORDER = ["정상", "1단계", "2단계 이상"]
ANY = "기회 신호 하나라도"


def _f(x: float) -> str:
    return "-" if x != x else f"{x:+.2f}%"


def run(ticker: str, start: str, horizons: tuple[int, ...]) -> int:
    store.init_db()
    close, signal_ids, directions, values, wh = wir.load_inputs(ticker)
    if close.empty:
        print(f"[{ticker}] 시세가 없습니다.")
        return 1
    states, is_on = wir.build_model_frame(
        close, signal_ids, directions, values, wh, dict(wi.DEFAULT_STAGE_WEIGHTS),
        wi.DEFAULT_COMBO_STAGE, wir.rs.DEFAULT_RISK_THRESHOLD, wir.rs.DEFAULT_SELL_STAGE_2_THRESHOLD,
    )
    mask = (close.index >= pd.Timestamp(start)) & states["has_weights"].to_numpy()
    dates = close.index[mask]
    group = states.loc[dates, "stage"].map(STATE_LABELS)
    fwd = {h: sb.forward_returns(close, h).loc[dates] for h in horizons}

    opp_ids = [sid for sid in signal_ids if directions[sid] == "opportunity" and is_on[sid].notna().any()]
    on_by = {sid: is_on.loc[dates, sid].fillna(0).astype(bool) for sid in opp_ids}
    if on_by:
        on_by[ANY] = pd.concat(on_by.values(), axis=1).any(axis=1)

    print(f"[{ticker}] {dates[0].date()} ~ {dates[-1].date()} · {len(dates)}거래일 "
          f"(weight_history 가중치가 있는 날만)")
    print("위험 상태 = 분리 방식 위험 점수(기회 신호 제외)로 판정. 상태별 날짜 수: "
          + " · ".join(f"{g} {int((group == g).sum())}일" for g in STATE_ORDER))
    print("이벤트 = 연속으로 켜진 구간의 첫날(그 날 종가 기준 이후 수익률). '상태 전체 평균'은 신호와 무관한 기준.\n")

    for sid, on in on_by.items():
        print(f"■ {sid}")
        print(f"  {'상태':8s} {'기간':>5s} {'이벤트n':>7s} {'켜진날':>6s} {'평균':>8s} {'중앙값':>8s} "
              f"{'최대손실':>9s} {'상승비율':>7s} {'상태전체평균':>10s} {'(n)':>6s}")
        for st in rsh.forward_stats_by_group(on, group, fwd, STATE_ORDER):
            warn = "" if st.reliable else "  ⚠ n<30"
            pos = "-" if st.positive_pct != st.positive_pct else f"{st.positive_pct:.0f}%"
            print(f"  {st.group:8s} {st.horizon:>4d}일 {st.n_events:>7d} {st.n_days:>6d} {_f(st.mean_return_pct):>8s} "
                  f"{_f(st.median_return_pct):>8s} {_f(st.worst_return_pct):>9s} {pos:>7s} "
                  f"{_f(st.group_all_days_mean_pct):>10s} {st.group_all_days_n:>6d}{warn}")
        print()

    print("과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. 이 결과는 매수·매도 추천이 아닙니다. "
          "표본수가 적은 칸(n<30)은 참고하지 말 것.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="기회 신호 이후 수익률을 위험 상태별로 (DB 읽기만)")
    p.add_argument("--ticker", default=wir.DEFAULT_TICKER)
    p.add_argument("--start", default=wir.DEFAULT_CONTEXT_START)
    p.add_argument("--horizons", default="5,20,63")
    args = p.parse_args()
    sys.exit(run(args.ticker, args.start, tuple(int(x) for x in args.horizons.split(","))))


if __name__ == "__main__":
    main()
