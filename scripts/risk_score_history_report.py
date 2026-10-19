"""
과거 위험 점수 시계열을 재구성해서, 점수 구간별로 이후 5/20/63거래일
수익률 분포(표본수·평균·중앙값·최대손실)를 보여주는 리포트(5단계).

analysis/risk_score_history.py가 순수 계산을 맡고, 이 스크립트는 DB에서
읽어서 그 모양으로 바꿔주는 얇은 배선이다. **미래 참조 없음**: 날짜 D의
위험 점수는 weight_history에서 recomputed_on<=D인 가장 최근 가중치만
쓴다(analysis/risk_score_history.py의 weights_as_of 참고).

**이 프로젝트는 아직 scripts/recompute_weights.py를 한 번만 실제로
돌렸다**(2026-10-06) — 그래서 그 날짜 이전 전체 구간은 "냉시작 균등
가중치" 근사로 채워진다(지어낸 숫자가 아니라 이 시스템의 실제 냉시작
규칙을 그대로 적용한 것, 모듈 docstring 참고). 결과를 읽을 때 cold_start
비율이 얼마나 되는지(아래 출력 첫 줄)를 꼭 같이 볼 것 — 비율이 높으면
"실제 백테스트로 정해진 가중치가 반영된 기간"은 아직 짧다는 뜻이다.

사용:
    python scripts/risk_score_history_report.py
    python scripts/risk_score_history_report.py --ticker 000660
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import risk_score_history as rsh  # noqa: E402
from analysis import signal_alignment as sa  # noqa: E402
from analysis import signal_backtest as sb  # noqa: E402
from db import store  # noqa: E402

DEFAULT_TICKER = "000660"
HORIZONS = (5, 20, 63)


def build_is_on_matrix(signal_ids: list[str], index: pd.DatetimeIndex) -> pd.DataFrame:
    """
    결정일(index)별 is_on. 해외 세션 신호는 t일보다 이전 값만, 원천 시장 휴장일은 직전 값을
    유지한다(analysis/signal_alignment.py). 값을 구할 수 없는 날은 0(꺼짐)으로 계산한다.

    (2026-10-18 이전에는 같은 날짜로 reindex하고 없는 날을 0으로 채웠다 — 미국 t일 종가를
    한국 t일 결정에 쓰는 반나절 미래참조 + 미국 휴장일마다 신호가 꺼지는 문제가 있었다.)
    """
    values = {sid: store.get_signal_values(sid) for sid in signal_ids}
    is_on, _ = sa.align_signals(values, index)
    return is_on.fillna(0)


def run(ticker: str) -> None:
    """리포트 본체. scripts/backfill_weights.py도 백필 직후 이 함수로 위험 점수를 다시 재구성한다."""
    store.init_db()
    close = store.get_prices(ticker)
    if close.empty:
        print(f"[{ticker}] 시세가 없습니다. 먼저 수집하세요.")
        sys.exit(1)
    close = close.sort_index()

    signals_df = store.get_signals(enabled_only=True)
    directional = signals_df[signals_df["direction"].isin(["risk", "opportunity"])]
    signal_ids = list(directional["signal_id"])
    directions = dict(zip(directional["signal_id"], directional["direction"]))
    if not signal_ids:
        print("켜고 끄는(risk/opportunity) 신호가 없습니다. scripts/seed_signals.py 먼저 실행하세요.")
        sys.exit(1)

    is_on = build_is_on_matrix(signal_ids, close.index)

    wh = store.get_weight_history()
    if not wh.empty:
        wh = wh.copy()
        wh["recomputed_on"] = pd.to_datetime(wh["recomputed_on"])
    weights, cold_start = rsh.effective_weights_as_of(wh, close.index, signal_ids)

    score = rsh.reconstruct_score_series(is_on, weights, directions)

    cold_pct = cold_start.mean() * 100
    print(f"[{ticker}] {close.index[0].date()} ~ {close.index[-1].date()} · {len(close)}거래일")
    print(f"냉시작(실제 백테스트 가중치 기록이 아직 없던) 구간: {cold_start.sum()}일 ({cold_pct:.1f}%)")
    print("(냉시작 구간은 '모든 신호 동일 가중치' 근사를 쓴다 — 그 시점에 실제로 쓰인 가중치가 아니라 "
          "이 시스템의 냉시작 규칙을 그대로 적용한 추정치)\n")

    forward_by_horizon = {h: sb.forward_returns(close, h) for h in HORIZONS}
    results = rsh.bucket_forward_returns(score, forward_by_horizon)

    print(f"{'구간':>8s} {'호라이즌':>8s} {'n':>6s} {'평균':>9s} {'중앙값':>9s} {'최악(최대손실)':>14s}")
    print("-" * 64)
    for r in results:
        warn = "" if r.reliable else "  ⚠ n<30"
        mean_s = f"{r.mean_return_pct:+.2f}%" if r.mean_return_pct == r.mean_return_pct else "-"
        median_s = f"{r.median_return_pct:+.2f}%" if r.median_return_pct == r.median_return_pct else "-"
        worst_s = f"{r.worst_return_pct:+.2f}%" if r.worst_return_pct == r.worst_return_pct else "-"
        print(f"{r.bucket_label:>8s} {r.horizon:>6d}일 {r.n:>6d} {mean_s:>9s} {median_s:>9s} {worst_s:>14s}{warn}")

    print(
        "\n과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. "
        "이 결과는 매수·매도 추천이 아닙니다. 표본수가 적은 구간(n<30)은 참고하지 말 것."
    )


def main() -> None:
    p = argparse.ArgumentParser(description="과거 위험 점수 시계열 재구성 + 점수 구간별 이후 수익률 분포")
    p.add_argument("--ticker", default=DEFAULT_TICKER)
    args = p.parse_args()
    run(args.ticker)


if __name__ == "__main__":
    main()
