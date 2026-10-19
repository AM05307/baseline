"""
신호 가중치 재계산 CLI (2026-10-06 3차 세션: 통계적 유의성 기반으로 교체).

**Walk-forward 검증**: 가중치는 in-sample(검증 제외 기간, 최근 1년 이전)
성적만으로 정한다 — "t 시점까지의 데이터로만 가중치를 산정"한다는 뜻에서
in-sample 구간 끝이 곧 t다. 그 가중치가 실제로 out-of-sample(최근 1년)
에서도 유지되는지는 별도로 표시만 한다(가중치 계산에는 안 씀) — 이게
진짜 walk-forward의 핵심: "미래(OOS) 성적을 보고 가중치를 거꾸로 맞추면"
의미가 없어진다.

가중치 규칙(신버전, analysis/weighting.compute_weights_significant):
신뢰구간 하한이 기준률 이하이거나 이벤트 수가 min_events(기본 30) 미만이면
가중치 0. 다중비교 보정(BH q-value)은 여기서 **가중치에 반영하지 않는다**
— 사용자가 지정한 가중치 규칙 자체가 신뢰구간·이벤트수만 쓰기 때문이다.
q-value는 참고용으로 표에만 같이 보여준다(scripts/signal_scorecard.py가
전체 신호×기간에 대해 더 폭넓게 보여준다 — 여긴 20거래일 하나만 본다).

매주 일요일 자동 실행을 염두에 뒀다(README "스케줄 등록" 참고).

사용:
    python scripts/recompute_weights.py
    python scripts/recompute_weights.py --dry-run   # DB에 안 쓰고 미리보기만
    python scripts/recompute_weights.py --min-events 20   # 최소 이벤트 수 조정
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import risk_score as rs  # noqa: E402
from analysis import signal_alignment as sa  # noqa: E402
from analysis import signal_backtest as sb  # noqa: E402
from analysis import weight_backfill as wb  # noqa: E402
from analysis import weighting as wt  # noqa: E402
from db import store  # noqa: E402

HYNIX = "000660"
PRIMARY_HORIZON = sb.PRIMARY_HORIZON  # 20거래일


def _fmt_pct(x: float) -> str:
    return f"{x:.1f}%" if x == x else "-"


def main() -> None:
    p = argparse.ArgumentParser(description="신호 가중치 재계산 (walk-forward, 매주 일요일 실행 권장)")
    p.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 미리보기만")
    p.add_argument("--min-events", type=int, default=wt.DEFAULT_MIN_EVENTS_FOR_WEIGHT,
                   help="가중치 산정에 필요한 최소 이벤트 수 (기본 30)")
    args = p.parse_args()

    store.init_db()
    close = store.get_prices(HYNIX)
    if close.empty:
        print(f"[{HYNIX}] 시세가 없습니다. 먼저 수집하세요.")
        sys.exit(1)

    signals_df = store.get_signals(enabled_only=True)
    if signals_df.empty:
        print("등록된(enabled=1) 신호가 없습니다. python scripts/seed_signals.py 먼저 실행하세요.")
        sys.exit(1)

    # 계산 본체는 analysis/weight_backfill.compute_weights_as_of — 과거 백필
    # (scripts/backfill_weights.py)과 같은 함수를 as_of=None(전체 데이터)으로 부른다.
    is_on_by_signal = {}
    for sid in signals_df["signal_id"]:
        values = store.get_signal_values(sid)
        # 해외 세션 신호는 한국 t일 결정에 t일 이전 값만, 원천 휴장일은 직전 값 유지(analysis/signal_alignment.py)
        if not values.empty:
            is_on_by_signal[sid] = sa.aligned_is_on(sid, values, close.sort_index().index)
    snapshot = wb.compute_weights_as_of(
        close, signals_df.to_dict("records"), is_on_by_signal, as_of=None, min_events=args.min_events,
    )
    in_sample_perf = snapshot.in_sample
    out_of_sample_perf = snapshot.out_of_sample
    all_signal_ids = snapshot.signal_ids
    new_weights = snapshot.weights
    old_weights = store.get_current_weights()

    today = date.today().isoformat()
    print(f"[{today}] 신호 가중치 재계산 — walk-forward(가중치: in-sample만, OOS는 비교 표시)")
    print(f"min_events={args.min_events} (이벤트 수 미만이면 가중치 0)")
    header = (
        f"{'신호':22s} {'n(IS)':>6s} {'적중률(IS)':>9s} {'기준률':>7s} {'CI하한':>7s} "
        f"{'q값':>7s} {'n(OOS)':>7s} {'적중률(OOS)':>10s} {'이전w':>7s} {'신규w':>7s}"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for sid in sorted(all_signal_ids):
        is_perf = in_sample_perf.get(sid)
        oos_perf = out_of_sample_perf.get(sid)
        old_w = old_weights.get(sid)
        new_w = new_weights.get(sid, 0.0)
        n_is = is_perf.n_events if is_perf else 0
        hr_is = is_perf.hit_rate_pct_events if is_perf else float("nan")
        base = is_perf.base_rate_pct if is_perf else float("nan")
        ci_lo = is_perf.ci_lower_pct if is_perf else float("nan")
        q = is_perf.q_value if is_perf else float("nan")
        n_oos = oos_perf.n_events if oos_perf else 0
        hr_oos = oos_perf.hit_rate_pct_events if oos_perf else float("nan")

        print(
            f"{sid:22s} {n_is:>6d} {_fmt_pct(hr_is):>9s} {_fmt_pct(base):>7s} {_fmt_pct(ci_lo):>7s} "
            f"{(f'{q:.3f}' if q == q else '-'):>7s} {n_oos:>7d} {_fmt_pct(hr_oos):>10s} "
            f"{(f'{old_w:.1f}%' if old_w is not None else '-'):>7s} {new_w:>6.1f}%"
        )
        rows.append({
            "recomputed_on": today, "signal_id": sid,
            "old_weight_pct": old_w, "new_weight_pct": new_w,
            "reason": (
                f"in_sample n_events={n_is} hit_rate={hr_is:.1f}% base_rate={base:.1f}% "
                f"ci_lower={ci_lo:.1f}% q={q:.3f}" if is_perf else "no signal history yet"
            ),
        })

    # --- 위험 점수 비교(변경 전/후 가중치, 오늘 켜진 신호 기준) ---
    today_on: dict[str, dict] = {}
    combo_value = None
    for sid in all_signal_ids:
        values = store.get_signal_values(sid)
        if values.empty:
            continue
        latest = values.sort_values("date").iloc[-1]
        if sid == "KHJ_COMBO":
            combo_value = latest["value"]
        if bool(latest["is_on"]):
            today_on[sid] = {}

    direction_by_sid = dict(zip(signals_df["signal_id"], signals_df["direction"]))
    for sid in today_on:
        today_on[sid]["direction"] = direction_by_sid[sid]

    def _risk_state_with(weights: dict[str, float]):
        on = {sid: {**meta, "weight_pct": weights.get(sid, 0.0)} for sid, meta in today_on.items()}
        return rs.build_risk_state(on, combo_value=combo_value)

    before = _risk_state_with({sid: (old_weights.get(sid) or 0.0) for sid in all_signal_ids})
    after = _risk_state_with(new_weights)
    print("\n위험 점수 비교 (오늘 켜진 신호 기준)")
    print(f"  변경 전: {before.risk_score:.1f}점 · {before.new_buy_status}"
          f"{' · ' + before.sell_stage_if_holding if before.sell_stage_if_holding else ''}")
    print(f"  변경 후: {after.risk_score:.1f}점 · {after.new_buy_status}"
          f"{' · ' + after.sell_stage_if_holding if after.sell_stage_if_holding else ''}")

    if args.dry_run:
        print("\n--dry-run: DB에 기록하지 않았습니다.")
        return

    store.insert_weight_history(rows)
    print(f"\nweight_history에 {len(rows)}건 기록됨.")


if __name__ == "__main__":
    main()
