"""
기술적 지표 규칙 백테스트 CLI.

과거 데이터에 규칙을 그대로 적용했을 때의 성과를 측정한다. 주문/매매는
절대 실행하지 않는다 — 조회+계산만 한다(CLAUDE.md 원칙 2). 실시간 "오늘의
신호"도 출력하지 않는다 — 이번 단계는 과거 데이터 백테스트뿐이다.

--strategy all (기본): SMA/RSI/볼린저/MACD 기반 보유 규칙 5개 + buy&hold.
--strategy pullback: 눌림목 지정가 매매(analysis/pullback.py) — 손절 있는
버전/없는 버전을 나란히 비교하고, 장세(상승추세/박스권/하락추세)별 성과도
따로 보여준다.
--strategy price_rules: 매수/목표/손절 기준가 규칙 3개(analysis/price_rules.py)
백테스트 + 통과 판정(CLAUDE.md 원칙 1 갱신판: 거래 100건 이상·전체·최근
1년 거래당 평균 둘 다 양수). 통과한 규칙만 오늘의 기준가를 출력한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import price_rule_backtest as prb  # noqa: E402
from analysis import price_rules  # noqa: E402
from analysis import pullback as pb  # noqa: E402
from analysis import rule_backtest as rb  # noqa: E402
from analysis import strategies as strat  # noqa: E402
from db import store  # noqa: E402

DEFAULT_TICKER = "000660"
MIN_ROWS = 250  # 대략 1년 거래일. 이보다 적으면 70/30 분할·n>=30 둘 다 의미가 약해진다.

# 2005-01-01을 기본 시작일로 둔다 — 000660은 2001년 전후(하이닉스 구조조정기)에
# 일봉 수정주가가 비정상적으로 튀는 날이 있다(2001-11-12 +32.08%, 당시 KRX
# 일일 가격제한(±15%)을 넘는 값 — 데이터 보정 흔적으로 추정, 확인 안 됨).
# 2005년 이후는 그런 이상치가 없는 걸 확인했다. --start로 언제든 바꿀 수 있다.
DEFAULT_START = "2005-01-01"

SPLIT_TITLES = {
    "full": "전체 기간",
    "in_sample": "In-sample (앞 70%)",
    "out_of_sample": "Out-of-sample (뒤 30%)",
}

REGIME_LABELS = {"uptrend": "상승추세", "box": "박스권", "downtrend": "하락추세"}

DISCLAIMER = (
    "\n과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. "
    "이 결과는 매수·매도 추천이 아닙니다."
)


def _cost_line() -> str:
    return (
        f"\n거래비용 가정: 편도 수수료 {rb.DEFAULT_COMMISSION_RATE * 100:.3f}%, "
        f"매도세 {rb.DEFAULT_TAX_RATE * 100:.2f}% (실제 요율은 증권사·연도별로 다르니 확인 필요)"
    )


def _build_strategies(close: pd.Series, threshold: float) -> list[tuple[str, pd.Series]]:
    rows = [
        ("ma_cross", strat.ma_cross(close)),
        ("rsi_reversion", strat.rsi_reversion(close)),
        ("bollinger_reversion", strat.bollinger_reversion(close)),
        ("macd_cross", strat.macd_cross(close)),
        (f"ensemble(threshold={threshold})", strat.ensemble(close, threshold=threshold)),
    ]
    if threshold != 0.75:
        # 과최적화 방지 목적(CLAUDE.md 참고): 파라미터를 데이터에 맞춰 고르지 않기 위해
        # 요청한 threshold와 무관하게 0.5/0.75 둘 다 항상 나란히 보여준다.
        rows.append(("ensemble(threshold=0.75)", strat.ensemble(close, threshold=0.75)))
    rows.append(("buy_and_hold", pd.Series(1, index=close.index)))
    return rows


def _print_table(title: str, results: list[tuple[str, rb.RuleBacktestResult]]) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")
    header = (
        f"{'전략':28s} {'총수익률':>9s} {'연환산':>9s} {'MDD':>9s} {'n':>5s} "
        f"{'승률':>7s} {'평균수익':>9s} {'평균손실':>9s} {'최악거래':>9s} {'노출도':>7s}"
    )
    print(header)
    print("-" * len(header))
    for name, r in results:
        warn = "  ⚠ n<30 (참고용, 통계적 결론 아님)" if not r.reliable else ""
        print(
            f"{name:28s} {r.total_return_pct:>8.2f}% {r.cagr_pct:>8.2f}% {r.mdd_pct:>8.2f}% "
            f"{r.n:>5d} {r.win_rate:>6.1f}% {r.avg_win:>8.2f}% {r.avg_loss:>8.2f}% "
            f"{r.worst:>8.2f}% {r.exposure_pct:>6.1f}%{warn}"
        )


def _print_regime_table(title: str, breakdown: dict[str, pb.RegimeBreakdown]) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")
    # 평균손익(기대값)·중앙값·최악거래는 buy&hold와 무관하게 "이 규칙으로 거래
    # 하나를 했을 때의 성적" 자체를 보는 지표다(analysis/pullback.py의
    # regime_breakdown() docstring 참고). buy&hold는 별도 비교 기준(기회비용)이다.
    header = (
        f"{'장세':10s} {'n':>5s} {'승률':>7s} {'평균손익(기대값)':>14s} {'중앙값':>9s} "
        f"{'최악거래':>9s} {'총손익':>9s} {'MDD':>9s} {'buy&hold':>9s}"
    )
    print(header)
    print("-" * len(header))

    def fmt(x: float) -> str:
        return f"{x:>8.2f}%" if x == x else f"{'-':>8s}%"

    for key, label in REGIME_LABELS.items():
        b = breakdown[key]
        warn = "  ⚠ n<30 (참고용, 통계적 결론 아님)" if not b.reliable else ""
        print(
            f"{label:10s} {b.n:>5d} {fmt(b.win_rate_pct)} {fmt(b.mean_return_pct):>14s} "
            f"{fmt(b.median_return_pct)} {fmt(b.worst_pct)} {fmt(b.total_return_pct)} "
            f"{fmt(b.mdd_pct)} {fmt(b.buy_and_hold_pct)}{warn}"
        )


def _run_all_strategies(args: argparse.Namespace, close: pd.Series, open_: pd.Series) -> None:
    rows = _build_strategies(close, args.threshold)
    all_results = {
        name: rb.backtest_rule(close, holding, open_, label=name) for name, holding in rows
    }
    for split, title in SPLIT_TITLES.items():
        results = [(name, all_results[name][split]) for name, _ in rows]
        _print_table(f"[{args.ticker}] {title}", results)
    print(_cost_line())


PRICE_RULE_SPLIT_TITLES = {
    "full": "전체 기간",
    "in_sample": "검증 제외 기간",
    "out_of_sample": "최근 1년 (검증, out-of-sample)",
}


def _print_price_rule_table(
    title: str, results: list[tuple[str, rb.RuleBacktestResult, bool]]
) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")
    header = (
        f"{'규칙':20s} {'총수익률':>9s} {'MDD':>9s} {'n':>5s} "
        f"{'승률':>7s} {'거래당평균':>9s} {'최악거래':>9s} {'통과':>6s}"
    )
    print(header)
    print("-" * len(header))
    for name, r, ok in results:
        warn = "  ⚠ n<30 (참고용)" if not r.reliable else ""
        print(
            f"{name:20s} {r.total_return_pct:>8.2f}% {r.mdd_pct:>8.2f}% "
            f"{r.n:>5d} {r.win_rate:>6.1f}% {r.mean_return:>8.2f}% "
            f"{r.worst:>8.2f}% {'PASS' if ok else 'FAIL':>6s}{warn}"
        )


def _run_price_rules(args: argparse.Namespace, ohlc: pd.DataFrame) -> None:
    """
    analysis/price_rules.py 3개 규칙을 백테스트하고, CLAUDE.md 원칙 1(갱신판)의
    통과 기준(n>=100, 전체·최근 1년 거래당 평균 둘 다 양수)을 만족하는지
    표시한다. 통과한 규칙만 "오늘의 매수/목표/손절 기준가"를 함께 보여준다
    — 통과 못 한 규칙은 가격을 보여주지 않는다(원칙 1 그대로 적용).
    """
    close = ohlc["close"]
    rows = []
    for name, spec in price_rules.RULES.items():
        levels = spec["fn"](ohlc)
        results = prb.backtest_price_rule(ohlc, levels, entry_type=spec["entry_type"], label=spec["label"])
        ok = prb.passes(results)
        rows.append((name, spec, levels, results, ok))

    for split, title in PRICE_RULE_SPLIT_TITLES.items():
        table_rows = [(spec["label"], results[split], ok) for _, spec, _, results, ok in rows]
        _print_price_rule_table(f"[{args.ticker}] {title}", table_rows)

    print(f"\n{'=' * 100}\n[{args.ticker}] 오늘의 기준가 (통과한 규칙만)\n{'=' * 100}")
    passing = [(name, spec, levels) for name, spec, levels, _, ok in rows if ok]
    if not passing:
        print("오늘 해당 규칙 없음 (백테스트 통과 기준을 만족한 규칙이 없습니다)")
    else:
        for name, spec, levels in passing:
            e, t, s = levels["entry_price"].iloc[-1], levels["target_price"].iloc[-1], levels["stop_price"].iloc[-1]
            if e != e:  # NaN -> 오늘은 신호 없음(통과한 규칙이라도 매일 신호가 나오진 않음)
                print(f"{spec['label']}: 오늘 신호 없음")
            else:
                print(f"{spec['label']}: 매수 {e:,.0f} / 목표 {t:,.0f} / 손절 {s:,.0f}")
    print(_cost_line())


def _run_pullback(args: argparse.Namespace, ohlc: pd.DataFrame) -> None:
    close = ohlc["close"]
    # (이름, stop_loss_pct, 거래비용 0으로 둘지) — 세 번째 변형(비용 0)으로
    # "비용 반영 전/후"를 같은 표(기대값·MDD·거래 수 전부 나란히)에서 바로
    # 비교할 수 있게 한다(2단계: 거래비용 파라미터화 요청).
    variants = [
        (f"pullback (손절 {args.stop_loss_pct * 100:.1f}%)", args.stop_loss_pct, False),
        ("pullback (손절 없음)", None, False),
        (f"pullback (손절 {args.stop_loss_pct * 100:.1f}%, 비용 0)", args.stop_loss_pct, True),
    ]
    all_results = {}
    trades_by_variant = {}
    for name, sl, zero_cost in variants:
        cost_kwargs = {"commission_rate": 0, "tax_rate": 0, "slippage_rate": 0} if zero_cost else {}
        trades = pb.simulate_pullback_trades(
            ohlc, dip_pct=args.dip_pct, gap_skip_pct=args.gap_skip_pct,
            take_profit_pct=args.take_profit_pct, stop_loss_pct=sl,
            max_hold_days=args.max_hold_days, **cost_kwargs,
        )
        trades_by_variant[name] = trades
        all_results[name] = pb.summarize_pullback_trades(close, trades, label=name)

    for split, title in SPLIT_TITLES.items():
        results = [(name, all_results[name][split]) for name, _, _ in variants]
        _print_table(f"[{args.ticker}] 눌림목 지정가 매매 · {title}", results)

    # 장세 구분 리포트는 전체 기간, 손절 있는(기본) 버전 기준으로만 낸다 —
    # 분할·버전을 더 쪼개면 장세별 표본이 더 희박해져서 경고만 늘어난다.
    base_name = variants[0][0]
    breakdown = pb.regime_breakdown(
        trades_by_variant[base_name], close,
        lookback=args.trend_lookback, up_threshold_pct=args.trend_up_threshold,
        down_threshold_pct=args.trend_down_threshold,
    )
    _print_regime_table(
        f"[{args.ticker}] 장세별 성과 (전체 기간 · {base_name} 기준)", breakdown,
    )
    print(_cost_line())
    print(
        "\n비용 0 변형(마지막 행)과 기본 변형을 비교하면 거래비용 자체의 효과를 "
        "볼 수 있다. 더 깊은 원인 분해(비용/복리/최악거래 기여도)는 "
        "python scripts/decompose_pullback.py 참고."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="기술적 지표 규칙 백테스트 (과거 데이터 측정만, 매매 실행 없음)"
    )
    parser.add_argument("--ticker", default=DEFAULT_TICKER)
    parser.add_argument(
        "--start", default=DEFAULT_START,
        help=f"YYYY-MM-DD (기본 {DEFAULT_START} — 그 이전 000660 데이터의 이상치 구간을 "
             "피하려는 기본값, --start ''로 비우면 전체 기간 사용)",
    )
    parser.add_argument("--end", default=None, help="YYYY-MM-DD")
    parser.add_argument(
        "--strategy", choices=["all", "pullback", "price_rules"], default="all",
        help="all(기본): 지표 보유 규칙 5개+buy&hold. pullback: 눌림목 지정가 매매. "
             "price_rules: 매수/목표/손절 기준가 규칙(analysis/price_rules.py) 백테스트+통과 판정",
    )
    parser.add_argument(
        "--threshold", type=float, default=strat.DEFAULT_ENSEMBLE_THRESHOLD,
        help="(--strategy all) ensemble 보유 임계값 (기본 0.5, 4개 중 2개 이상 동의)",
    )
    parser.add_argument("--dip-pct", type=float, default=pb.DEFAULT_DIP_PCT,
                         help="(--strategy pullback) 전일 종가 대비 지정가 매수 할인율 (기본 0.02)")
    parser.add_argument("--gap-skip-pct", type=float, default=pb.DEFAULT_GAP_SKIP_PCT,
                         help="(--strategy pullback) 이 이상 갭상승이면 그날 주문 안 냄 (기본 0.02)")
    parser.add_argument("--take-profit-pct", type=float, default=pb.DEFAULT_TAKE_PROFIT_PCT,
                         help="(--strategy pullback) 익절 비율 (기본 0.02)")
    parser.add_argument("--stop-loss-pct", type=float, default=pb.DEFAULT_STOP_LOSS_PCT,
                         help="(--strategy pullback) 손절 비율 (기본 0.03). 손절 없는 버전은 항상 같이 출력됨")
    parser.add_argument("--max-hold-days", type=int, default=pb.DEFAULT_MAX_HOLD_DAYS,
                         help="(--strategy pullback) 최대 보유일, 지나면 종가 강제 청산 (기본 10)")
    parser.add_argument("--trend-lookback", type=int, default=pb.TREND_LOOKBACK_DAYS,
                         help="(--strategy pullback) 장세 분류용 직전 거래일 수 (기본 20)")
    parser.add_argument("--trend-up-threshold", type=float, default=pb.TREND_UP_THRESHOLD_PCT,
                         help="(--strategy pullback) 이 %% 초과면 상승추세 (기본 8.0)")
    parser.add_argument("--trend-down-threshold", type=float, default=pb.TREND_DOWN_THRESHOLD_PCT,
                         help="(--strategy pullback) 이 %% 미만이면 하락추세 (기본 -8.0)")
    args = parser.parse_args()

    store.init_db()
    ohlc = store.get_ohlc(args.ticker)
    if ohlc.empty or len(ohlc) < MIN_ROWS:
        print(f"[{args.ticker}] 데이터가 {len(ohlc)}일뿐입니다. 먼저 아래를 실행하세요:")
        print("    python collectors/kr_market.py 5y   # 또는 max")
        print("  (해외 종목이면 collectors/us_market.py를 같은 방식으로)")
        sys.exit(1)

    close = ohlc["close"].dropna().sort_index()
    if args.start:
        close = close.loc[close.index >= pd.Timestamp(args.start)]
    if args.end:
        close = close.loc[close.index <= pd.Timestamp(args.end)]

    if len(close) < MIN_ROWS:
        print(f"[{args.ticker}] --start/--end로 좁힌 구간이 {len(close)}일뿐입니다. 기간을 넓혀주세요.")
        sys.exit(1)

    ohlc_filtered = ohlc.loc[close.index]

    if args.strategy == "pullback":
        _run_pullback(args, ohlc_filtered)
    elif args.strategy == "price_rules":
        _run_price_rules(args, ohlc_filtered)
    else:
        _run_all_strategies(args, close, ohlc_filtered["open"])

    print(DISCLAIMER)


if __name__ == "__main__":
    main()
