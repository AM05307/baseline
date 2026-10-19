"""
"최근 기간에 위험 점수 모델대로 비중을 조절했다면 어땠을지" 가상 백테스트.

**주문 API는 호출하지 않는다**(원칙 2) — DB에 이미 있는 시세·신호값·
weight_history만 읽어서 계산하고 출력한다. DB에 쓰지 않는다. 계산 본체는
analysis/what_if.py(순수 함수), 이 스크립트는 DB를 읽어 그 모양으로 바꾸는
얇은 배선이다.

전제: 먼저 시세와 신호값이 최신이어야 한다.
    python collectors/collect_daily.py
    python scripts/compute_signals.py
(가중치 재계산(recompute_weights.py)은 하지 않는다 — 이 스크립트는 각 결정일
이전에 이미 저장돼 있던 weight_history만 쓴다.)

비교 대상:
    A. 모델 비중 조절 (t일 종가의 상태 → t+1일 비중)
    B. 단순 보유 (100%)
    C. 전액 현금 (0%)

사용:
    python scripts/what_if_recent.py
    python scripts/what_if_recent.py --start 2026-10-05 --end 2026-10-16
    python scripts/what_if_recent.py --stage-weights 1,0.5,0.25,0
    python scripts/what_if_recent.py --context-window 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import risk_score as rs  # noqa: E402
from analysis import signal_alignment as sa  # noqa: E402
from analysis import trading_costs as tc  # noqa: E402
from analysis import what_if as wi  # noqa: E402
from db import store  # noqa: E402

DEFAULT_TICKER = "000660"
DEFAULT_START = "2026-10-05"
DEFAULT_CONTEXT_START = "2005-01-01"

# 해외 세션 신호 목록·정렬 규칙은 analysis/signal_alignment.py 한 곳에서 관리한다.
PRIOR_SESSION_SIGNALS = sa.PRIOR_SESSION_SIGNALS

DISCLAIMER = (
    "과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. "
    "이 결과는 매수·매도 추천이 아닙니다."
)


def _parse_stage_weights(text: str) -> dict[int, float]:
    parts = [float(x) for x in text.split(",")]
    if len(parts) != 4 or not all(0 <= p <= 1 for p in parts):
        raise argparse.ArgumentTypeError("정상,1단계,2단계,3단계 비중 4개를 0~1 사이로 (예: 1,0.66,0.33,0)")
    return dict(enumerate(parts))


def _pct(x: float, signed: bool = True) -> str:
    if x != x:  # NaN
        return "데이터 없음"
    return f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%"


def load_inputs(ticker: str):
    close = store.get_prices(ticker).sort_index()
    signals_df = store.get_signals(enabled_only=True)
    directional = signals_df[signals_df["direction"].isin(["risk", "opportunity"])]
    signal_ids = list(directional["signal_id"])
    directions = dict(zip(directional["signal_id"], directional["direction"]))
    values = {sid: store.get_signal_values(sid) for sid in signal_ids}
    wh = store.get_weight_history()
    return close, signal_ids, directions, values, wh


def load_opens(ticker: str) -> pd.Series:
    """시가(체결 시점 next_open용). 없으면 빈 Series."""
    ohlc = store.get_ohlc(ticker)
    return ohlc["open"].sort_index() if not ohlc.empty else pd.Series(dtype=float)


def kr_calendar() -> pd.DatetimeIndex:
    """그룹 방식 기준점을 정할 한국 거래일 달력(KOSPI 시세 날짜). 대상 종목 시세가 짧아도(069500은 2007~) 같은 기준 기간을 쓰게."""
    k = store.get_prices("KOSPI")
    return k.index if not k.empty else pd.DatetimeIndex([])


def build_model_frame(close, signal_ids, directions, values, wh, stage_weights, combo_stage,
                      risk_threshold, sell_stage_2_threshold, net_opportunity=False,
                      method=wi.DEFAULT_SCORE_METHOD, calibration_dates=None, exclude=frozenset()):
    """
    전체 거래일에 대해 결정일 기준 점수·상태·목표 비중·냉시작 여부를 만든다.
    method="group"(기본): 신호 그룹 균등 가중치 + 기준 기간 백분위 단계(analysis/group_score). 기준점은
    states.attrs["thresholds"]. "weighted": 예전 적중률 가중치(weight_history) + 60/80점.
    """
    dates = close.index
    if method == "group":
        grid = dates.union(calibration_dates) if calibration_dates is not None and len(calibration_dates) else dates
        is_on_all, _ = wi.align_signals(values, grid, PRIOR_SESSION_SIGNALS)
        states = wi.group_states(is_on_all, dates, exclude=exclude)
        states["weight_next"] = states["stage"].map(stage_weights)
        states["has_weights"] = states["groups_with_data"] > 0  # 그룹 방식은 가중치 기록이 필요 없다 — 값 있는 그룹만 있으면 됨
        is_on = is_on_all.reindex(dates)
        states["n_signals_with_value"] = is_on.notna().sum(axis=1)
        states["n_signals"] = len(signal_ids)
        return states, is_on
    if method != "weighted":
        raise ValueError(f"method는 {wi.SCORE_METHODS} 중 하나: {method}")
    is_on, value = wi.align_signals(values, dates, PRIOR_SESSION_SIGNALS)
    weights = wi.weights_for_dates(wh, dates, signal_ids)
    combo = value["KHJ_COMBO"] if "KHJ_COMBO" in value.columns else None
    states = wi.score_and_stage(is_on, weights, directions, combo, risk_threshold,
                                sell_stage_2_threshold, combo_stage, net_opportunity)
    states["weight_next"] = states["stage"].map(stage_weights)
    states["has_weights"] = weights.notna().any(axis=1)
    states["n_signals_with_value"] = is_on.notna().sum(axis=1)
    states["n_signals"] = len(signal_ids)
    return states, is_on


def run(args) -> int:
    store.init_db()
    close, signal_ids, directions, values, wh = load_inputs(args.ticker)
    if close.empty:
        print(f"[{args.ticker}] 시세가 없습니다. 먼저 python collectors/collect_daily.py 실행.")
        return 1
    if not signal_ids:
        print("켜고 끄는(risk/opportunity) 신호가 없습니다. scripts/seed_signals.py 먼저 실행.")
        return 1

    last_day = close.index[-1]
    end = pd.Timestamp(args.end) if args.end else last_day
    start = pd.Timestamp(args.start)
    period = close.index[(close.index >= start) & (close.index <= end)]
    if len(period) == 0:
        print(f"{start.date()} ~ {end.date()} 사이 거래일이 없습니다.")
        return 1
    first_pos = close.index.get_loc(period[0])
    if first_pos == 0:
        print("기간 첫날 이전 거래일이 없어 첫날 비중을 정할 수 없습니다.")
        return 1

    print(f"[{args.ticker}] 수집된 마지막 거래일: {last_day.date()}")
    weekdays = pd.bdate_range(start, end)
    missing = [d.date().isoformat() for d in weekdays if d not in close.index]
    print(f"기간: {period[0].date()} ~ {period[-1].date()} · {len(period)}거래일 "
          f"(요청 {start.date()} ~ {end.date()}; 시세 없는 평일 = 휴장: {', '.join(missing) or '없음'})")

    try:
        states, _ = build_model_frame(close, signal_ids, directions, values, wh, args.stage_weights,
                                      args.combo_stage, args.risk_threshold, args.stage2_threshold,
                                      args.legacy_netting, method=args.method, calibration_dates=kr_calendar())
    except ValueError as e:  # 그룹 방식: 기준 기간에 점수가 없음(데이터 부족) — 기준점을 지어내지 않는다
        print(f"상태를 계산할 수 없습니다: {e}. 기준 기간을 덮는 시세·신호값을 수집하거나 --method weighted를 쓰세요.")
        return 1
    if args.method == "group":
        thr = states.attrs["thresholds"]
        print("점수 방식: 신호 그룹 균등 가중치(기본, --method group) — 상태는 위험점수로만, 기준점 = 기준 기간 "
              f"{thr.start}~{thr.end} 분포 상위 " + "/".join(f"{q:.0%}" for q in thr.quantiles))
        print("  기준점(1/2/3단계): " + " / ".join("도달 불가" if v == float("inf") else f"{v:.1f}" for v in thr.values)
              + " · 기준 기간 실제 비율 " + " / ".join(f"{a:.1%}" for a in thr.actual_shares) + f" (n={thr.n_days}일)")
    else:
        print("점수 방식: 적중률 가중치(옵션, --method weighted) — " + (
            "예전(위험점수 = risk − opportunity, --legacy-netting)" if args.legacy_netting
            else "분리(위험점수 = risk만, 기회점수 별도 — 상태는 위험점수로만)"))
    returns = close.pct_change()
    decision_date = pd.Series(close.index, index=close.index).shift(1)  # t일 비중을 정한 날 = 직전 거래일
    target = states["weight_next"].shift(1)  # t-1일 종가 상태 → t일 비중
    has_w = states["has_weights"].shift(1).fillna(False).astype(bool)

    if not has_w[period].all():
        bad = [d.date().isoformat() for d in period if not has_w[d]]
        print(f"이 날들의 결정일에 weight_history 기록이 없습니다(냉시작): {bad} — 중단.")
        return 1

    costs = wi.CostModel(args.commission, args.slippage, tc.tax_rate_on)
    prev_day = close.index[first_pos - 1]
    next_open = args.fill == "next_open"
    if next_open:
        opens = load_opens(args.ticker)
        rng = close.index[first_pos - 1:close.index.get_loc(period[-1]) + 1]
        missing_open = [d.date().isoformat() for d in rng[1:] if d not in opens.index or pd.isna(opens.get(d))]
        if missing_open:
            print(f"시가가 없는 날이 있어 next_open으로 계산할 수 없습니다: {missing_open}")
            return 1
        decided = states["weight_next"]
        sim_a = wi.simulate_next_open(close[rng], opens[rng], decided[rng], args.initial_weight, costs)
        sim_b = wi.simulate_next_open(close[rng], opens[rng], pd.Series(1.0, index=rng), 1.0, costs)
        sim_c = wi.simulate_next_open(close[rng], opens[rng], pd.Series(0.0, index=rng), 0.0, costs)
    else:
        sim_a = wi.simulate(returns[period], target[period], decision_date[period], args.initial_weight, costs)
        sim_b = wi.simulate(returns[period], pd.Series(1.0, index=period), decision_date[period], 1.0, costs)
        sim_c = wi.simulate(returns[period], pd.Series(0.0, index=period), decision_date[period], 0.0, costs)
    print("체결 시점: " + ("t일 종가 결정 → t+1일 시가 체결(기본, --fill next_open)" if next_open
                       else "t일 종가 결정 → t일 종가 체결(--fill same_close, 2026-10-19 이전 기본)"))
    s0 = states.loc[prev_day]
    print(f"\n첫날 비중은 {prev_day.date()} 종가 상태로 결정: 위험점수 {s0['risk_score']:.1f} · "
          f"{wi.STAGE_LABELS[s0['stage']]} → {s0['weight_next'] * 100:.0f}%")
    print(f"기간 시작 전 보유 비중 가정: A {args.initial_weight * 100:.0f}% · B 100% · C 0% "
          f"(기간 끝 청산 안 함 — 평가 기준)")
    print(f"거래비용: 수수료 {args.commission * 100:.3f}% · 매도세 {tc.tax_rate_on(period[0].date()) * 100:.2f}% "
          f"(analysis/trading_costs, 참고용 근사치) · 슬리피지 {args.slippage * 100:.3f}%")

    print("\n=== 일별 ===")
    print("위험점수·상태는 그날 종가 기준(다음 거래일 비중을 정함), 적용 비중은 전 거래일 상태로 정해진 그날 비중"
          + ("(그날 시가부터, 시가 갭은 이전 비중)." if next_open else "."))
    head = (f"{'날짜':10s} {'종가':>11s} {'일간':>8s} {'시가갭':>7s} {'위험점수':>7s} {'상태':>5s} {'적용비중':>7s} "
            f"{'A일간':>8s} {'B일간':>8s} {'A누적':>8s} {'B누적':>8s} {'기회점수':>7s} {'신호값':>6s}")
    print(head)
    print("-" * 120)
    for d in period:
        st = states.loc[d]
        cov = f"{int(st['n_signals_with_value'])}/{int(st['n_signals'])}"
        gap = f"{sim_a.loc[d, 'gap_pct']:+.2f}%" if next_open else "-"
        print(f"{d.date()!s:10s} {close[d]:>11,.0f} {_pct(returns[d]):>8s} {gap:>7s} {st['risk_score']:>7.1f} "
              f"{wi.STAGE_LABELS[st['stage']]:>5s} {sim_a.loc[d, 'weight'] * 100:>6.0f}% "
              f"{_pct(sim_a.loc[d, 'ret']):>8s} {_pct(sim_b.loc[d, 'ret']):>8s} "
              f"{_pct(sim_a.loc[d, 'equity'] - 1):>8s} {_pct(sim_b.loc[d, 'equity'] - 1):>8s} "
              f"{st['opportunity_score']:>7.1f} {cov:>6s}")
    print("(신호값 = 그날 값이 있는 신호 수 / 활성 신호 수. 값 없는 신호는 꺼진 것으로 계산됨)")

    print("\n켜진 신호(종가 기준):")
    for d in [prev_day, *period]:
        print(f"  {d.date()}: {', '.join(states.loc[d, 'on_signals']) or '없음'}")

    print("\n=== 기간 합계 ===")
    print(f"{'':16s} {'누적수익률':>10s} {'최대낙폭':>9s} {'거래비용':>9s} {'비중변경':>7s}")
    for name, sim in [("A. 모델 비중 조절", sim_a), ("B. 단순 보유", sim_b), ("C. 전액 현금", sim_c)]:
        s = wi.summarize(sim)
        print(f"{name:16s} {_pct(s.cum_return):>10s} {_pct(s.mdd):>9s} {_pct(s.total_cost, False):>9s} {s.n_changes:>6d}회")
    a_sum, b_sum = wi.summarize(sim_a), wi.summarize(sim_b)
    diff = a_sum.cum_return - b_sum.cum_return
    print(f"A − B = {diff * 100:+.2f}%p")
    print("(거래비용은 시작 자산 대비가 아니라 거래 직전 자산 대비 비율의 합)")

    window = args.context_window or len(period)
    print(f"\n=== 맥락: {args.context_start} 이후 모든 {window}거래일 구간의 A − B 분포 ===")
    eligible = has_w & (close.index >= pd.Timestamp(args.context_start)) & (close.index < period[0])
    eligible = pd.Series(eligible, index=close.index)
    if next_open:
        elig_dec = pd.Series(states["has_weights"].to_numpy() & (close.index >= pd.Timestamp(args.context_start)),
                             index=close.index)
        hist = wi.rolling_window_diffs_next_open(close, opens.reindex(close.index), states["weight_next"], elig_dec,
                                                 window, costs, args.initial_weight, end_before=period[0])
    else:
        hist = wi.rolling_window_diffs(returns, target, decision_date, eligible, window, costs, args.initial_weight)
    evidence = wi.snapshot_evidence(wh, close.index).shift(1)  # t일 비중을 정한 결정일(t-1)의 스냅샷
    recent_ev = {evidence.get(d) for d in period}
    if hist.empty:
        print("비교할 과거 구간이 없습니다(weight_history 기록이 있는 구간 없음).")
        pct = float("nan")
    else:
        ev_true = evidence.map(lambda v: v is True).astype(int).cumsum()
        pos = {d: i for i, d in enumerate(close.index)}
        def _all_evidence(row) -> bool:
            i, j = pos[row["start"]], pos[row["end"]]
            return int(ev_true.iloc[j] - (ev_true.iloc[i - 1] if i > 0 else 0)) == j - i + 1
        hist["evidence"] = hist.apply(_all_evidence, axis=1)
        nonzero = hist["diff"].abs() >= 1e-12
        dists = [
            wi.summarize_distribution("전체 (A−B=0 포함)", hist["diff"], diff, window),
            wi.summarize_distribution("A−B≠0 구간만", hist.loc[nonzero, "diff"], diff, window),
        ]
        if args.method == "weighted":  # 가중치 스냅샷 근거는 예전 방식에만 의미가 있다
            dists += [
                wi.summarize_distribution("데이터 근거 가중치 구간만 (0 포함)", hist.loc[hist["evidence"], "diff"], diff, window),
                wi.summarize_distribution("데이터 근거 & A−B≠0", hist.loc[hist["evidence"] & nonzero, "diff"], diff, window),
            ]
        pct = dists[0].percentile
        print(f"구간: {hist['start'].min().date()} ~ {hist['end'].max().date()}, 한 거래일씩 밀어 서로 겹침 "
              "(n은 겹치는 구간 수, '비겹침'은 n ÷ 구간 길이로 센 대략의 독립 구간 수)")
        if args.method == "weighted":
            print("조건: 구간 내 모든 날의 가중치가 weight_history(백필 포함)에서 왔고, 최근 기간과 겹치지 않음")
            print("'데이터 근거 가중치 구간' = 구간 내 모든 날의 가중치 스냅샷에 통계적으로 유의한 신호가 1개 이상 "
                  "(나머지는 균등 초기값 규칙)")
        else:
            print("조건: 구간 내 모든 결정일에 값 있는 위험 그룹이 1개 이상, 최근 기간과 겹치지 않음. "
                  "기준점은 기준 기간에서 정한 고정값이라 검증 구간(2019~)에서는 표본 외 판정")
        print(f"A − B = 0인 구간(모델이 내내 100% 보유): {(~nonzero).mean() * 100:.1f}%")
        print(f"\n{'분포':30s} {'n':>6s} {'비겹침':>6s} {'5%':>7s} {'25%':>7s} {'중앙값':>7s} {'75%':>7s} {'95%':>7s} {'최대':>7s} {'이번 백분위':>10s}")
        for ds in dists:
            if ds.n == 0:
                print(f"{ds.label:30s} {0:>6d} {0:>6d}   (표본 없음)")
                continue
            warn = "  ⚠ 비겹침 n<30" if ds.n_independent < 30 else ""
            print(f"{ds.label:30s} {ds.n:>6d} {ds.n_independent:>6d} {ds.q05*100:>+7.2f} {ds.q25*100:>+7.2f} "
                  f"{ds.q50*100:>+7.2f} {ds.q75*100:>+7.2f} {ds.q95*100:>+7.2f} {ds.max*100:>+7.2f} {ds.percentile:>9.1f}%{warn}")
        if args.method == "weighted":
            label = {True: "데이터 근거", False: "균등 초기값 규칙", None: "판별 불가"}
            print(f"(이번 기간 자체의 가중치 스냅샷: {', '.join(sorted(label[v] for v in recent_ev))})")
        print(f"이번 기간 A − B {diff * 100:+.2f}%p")

    print("\n=== 결론(사실만) ===")
    print(f"- {period[0].date()} ~ {period[-1].date()} {len(period)}거래일 동안 종가는 "
          f"{close[prev_day]:,.0f} → {close[period[-1]]:,.0f}.")
    print(f"- 모델 비중 조절(A) {_pct(a_sum.cum_return)}, 단순 보유(B) {_pct(b_sum.cum_return)}, "
          f"전액 현금(C) +0.00%. A − B = {diff * 100:+.2f}%p, 비중 변경 {a_sum.n_changes}회.")
    if pct == pct:
        print(f"- 이 차이는 과거 {len(hist)}개 {window}거래일 구간(서로 겹침, A−B=0 포함) 분포에서 "
              f"{pct:.1f}번째 백분위다. 0을 뺀 분포·데이터 근거 구간만의 분포는 위 표 참고.")
    print("- 이번 2주는 표본이 1개라 모델 성능 판단 근거가 아니다.")
    print(f"\n{DISCLAIMER}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="최근 기간 위험 점수 모델 비중 조절 가상 백테스트 (주문 없음)")
    p.add_argument("--ticker", default=DEFAULT_TICKER)
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=None, help="기본: 수집된 마지막 거래일")
    p.add_argument("--stage-weights", type=_parse_stage_weights,
                   default=dict(wi.DEFAULT_STAGE_WEIGHTS), help="정상,1단계,2단계,3단계 (기본 1,0.66,0.33,0)")
    p.add_argument("--combo-stage", type=int, choices=[1, 2, 3], default=wi.DEFAULT_COMBO_STAGE,
                   help="KHJ_COMBO 트리거를 몇 단계로 볼지 (기본 3)")
    p.add_argument("--risk-threshold", type=float, default=rs.DEFAULT_RISK_THRESHOLD)
    p.add_argument("--stage2-threshold", type=float, default=rs.DEFAULT_SELL_STAGE_2_THRESHOLD)
    p.add_argument("--initial-weight", type=float, default=1.0, help="기간 시작 전 A의 보유 비중 (기본 1.0)")
    p.add_argument("--commission", type=float, default=tc.DEFAULT_COMMISSION_RATE)
    p.add_argument("--slippage", type=float, default=tc.DEFAULT_SLIPPAGE_RATE)
    p.add_argument("--context-window", type=int, default=None, help="맥락 비교 구간 길이(거래일). 기본: 기간 길이")
    p.add_argument("--context-start", default=DEFAULT_CONTEXT_START)
    p.add_argument("--fill", choices=list(wi.FILL_MODES), default=wi.DEFAULT_FILL,
                   help="체결 시점: next_open(기본, t 종가 결정 → t+1 시가 체결) | same_close(t 종가 체결)")
    p.add_argument("--method", choices=list(wi.SCORE_METHODS), default=wi.DEFAULT_SCORE_METHOD,
                   help="group(기본): 신호 그룹 균등 가중치 + 백분위 단계 / weighted: 예전 적중률 가중치 + 60/80점")
    p.add_argument("--legacy-netting", action="store_true",
                   help="예전 점수 방식(opportunity가 위험점수를 깎음)으로 계산 — 분리 전/후 비교용")
    sys.exit(run(p.parse_args()))


if __name__ == "__main__":
    main()
