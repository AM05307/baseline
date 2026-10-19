"""
2005-01-01 이후 000660 데이터로 모든 신호를 백테스트하고 reports/
signal_scorecard.md로 저장하는 CLI.

**거래비용(수수료·세금) 반영 여부: 반영하지 않는다.** 이 스코어카드는 매매
시뮬레이션이 아니라 "신호가 on인 날 이후 N거래일 동안 가격이 어떻게
움직였는지"만 보는 관측이다 — 체결가도, 수수료도, 세금도 없다(살 것도
팔 것도 없으므로). 실제로 이 규칙대로 사고팔았을 때의 성과(거래비용 반영)를
보려면 analysis/price_rule_backtest.py나 analysis/pullback.py 같은 체결
시뮬레이션이 별도로 필요하다 — 이 스코어카드는 그 단계가 아니다.

**통계 검증(2026-10-06 3차 세션 추가)**: 적중률을 50%가 아니라 그 구간
"아무 날이나 찍었을 때"의 기준률과 비교하고(edge), 이벤트(연속 on 구간을
하나로 묶은 것) 단위 부트스트랩 95% 신뢰구간·이항검정 p-value를 낸다.
**다중비교 보정(q-value)은 같은 구간(full/in_sample/out_of_sample) 안의
모든 신호×기간 조합을 하나의 검정 묶음으로 보고 Benjamini-Hochberg로
계산한다** — 구간이 다르면(예: full과 out_of_sample) 서로 다른 데이터라
같은 묶음으로 보정하지 않는다.

주문/매매는 하지 않는다(원칙 2). 출력에 예측 문구를 넣지 않는다(원칙 1).

사용:
    python scripts/signal_scorecard.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import signal_alignment as sa  # noqa: E402
from analysis import signal_backtest as sb  # noqa: E402
from analysis import signals as sig  # noqa: E402
from db import store  # noqa: E402

HYNIX = "000660"
START_DATE = "2005-01-01"
HORIZONS = sb.DEFAULT_HORIZONS  # (5, 20, 63)
PRIMARY_HORIZON = sb.PRIMARY_HORIZON  # 20
WINDOWS = (("full", "전체"), ("in_sample", "검증제외"), ("out_of_sample", "최근1년"))

DISCLAIMER = (
    "과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. "
    "이 결과는 매수·매도 추천이 아닙니다."
)


def _fmt(x: float) -> str:
    return f"{x:+.2f}" if x == x else "-"


def _fmt_pct(x: float) -> str:
    return f"{x:.1f}%" if x == x else "-"


def _row_md(label: str, perf: sb.SignalPerformance) -> str:
    warn = " ⚠n<30" if not perf.reliable else ""
    sig_mark = " ✓유의" if perf.significant else ""
    return (
        f"| {label} | {perf.n} | {_fmt(perf.hit_rate_pct)}% | {_fmt(perf.mean_return_pct)}% | "
        f"{_fmt(perf.median_return_pct)}% | {_fmt(perf.worst_pct)}% | "
        f"{perf.n_events} | {_fmt_pct(perf.hit_rate_pct_events)} | {_fmt_pct(perf.base_rate_pct)} | "
        f"{_fmt_pct(perf.ci_lower_pct)}~{_fmt_pct(perf.ci_upper_pct)} | "
        f"{(f'{perf.p_value:.3f}' if perf.p_value == perf.p_value else '-')} | "
        f"{(f'{perf.q_value:.3f}' if perf.q_value == perf.q_value else '-')}"
        f"{warn}{sig_mark} |"
    )


def main() -> None:
    store.init_db()
    close = store.get_prices(HYNIX)
    if close.empty:
        print(f"[{HYNIX}] 시세가 없습니다. 먼저 수집하세요: python collectors/kr_market.py max")
        sys.exit(1)
    close = close.loc[close.index >= pd.Timestamp(START_DATE)]
    if len(close) < 250:
        print(f"[{HYNIX}] {START_DATE} 이후 데이터가 {len(close)}일뿐입니다.")
        sys.exit(1)

    signals_df = store.get_signals(enabled_only=True)
    scorable = signals_df[signals_df["direction"].isin(["risk", "opportunity"])]

    # 1패스: 전체 신호×기간 성적을 먼저 계산해둔다(다중비교 보정은 구간별로
    # 전체 신호×기간을 한 묶음으로 봐야 해서, 신호 하나씩 바로 출력할 수 없다).
    perf_by_signal: dict[str, dict[int, dict[str, sb.SignalPerformance]]] = {}
    low_sample_signals: list[str] = []
    top_n_candidates: list[tuple[str, int]] = []

    for _, row in scorable.iterrows():
        sid, direction = row["signal_id"], row["direction"]
        values = store.get_signal_values(sid)
        if values.empty:
            low_sample_signals.append(f"{sid} (n=0, 계산값 없음)")
            continue
        # 해외 세션 신호는 한국 t일 결정에 t일 이전 값만, 원천 휴장일은 직전 값 유지(analysis/signal_alignment.py)
        is_on = sa.aligned_is_on(sid, values, close.index)
        perf_by_signal[sid] = sb.evaluate_signal(is_on, close, direction=direction, horizons=HORIZONS)
        full_primary = perf_by_signal[sid][PRIMARY_HORIZON]["full"]
        top_n_candidates.append((sid, full_primary.n))
        if not full_primary.reliable:
            low_sample_signals.append(f"{sid} (n={full_primary.n})")

    # 다중비교 보정: 구간(윈도)별로 그 구간에 속한 모든 신호×기간 SignalPerformance를
    # 한 묶음으로 모아 BH를 한 번씩 돌린다.
    for window, _ in WINDOWS:
        bucket = [perf_by_signal[sid][h][window] for sid in perf_by_signal for h in HORIZONS]
        sb.apply_benjamini_hochberg(bucket)

    lines = [
        "# 신호 성적표 (signal scorecard)",
        "",
        f"생성일: {date.today().isoformat()} · 대상: {HYNIX} · 구간: {START_DATE} ~ {close.index[-1].date()}",
        "",
        "**거래비용(수수료·세금) 반영: 안 함** — 이 표는 매매 시뮬레이션이 아니라 "
        "'신호 on 다음 N거래일 가격 변화' 측정이다. 체결가·수수료·세금이 적용될 거래 "
        "자체가 없다(모듈 상단 docstring 참고).",
        "",
        "적중 정의: risk 신호는 이후 수익률 < 0, opportunity 신호는 > 0이면 적중. "
        "'사건(events)'은 연속 on 구간을 하나로 묶어 중복 표본을 줄인 버전. "
        "'기준률'은 같은 구간 아무 날이나 찍었을 때의 비율(0.5가 아님). "
        "'CI'는 이벤트 단위 부트스트랩 95% 신뢰구간. q값은 같은 구간(전체/검증제외/"
        "최근1년) 안의 모든 신호×기간을 한 묶음으로 본 Benjamini-Hochberg 보정값. "
        "'✓유의'는 신뢰구간 하한이 기준률을 넘고 이벤트 수가 최소 기준(기본 30) "
        "이상일 때만 표시 — 가중치를 받는 조건과 같다(analysis/weighting.py).",
        "",
    ]

    for _, row in scorable.iterrows():
        sid, direction, label = row["signal_id"], row["direction"], row["name"]
        lines.append(f"## {sid} — {label} ({direction})")
        lines.append("")
        lines.append(row["description"])
        lines.append("")
        if sid not in perf_by_signal:
            lines.append("_데이터 없음 — 아직 계산되지 않았거나(수동 입력 필요) 입력 전 상태._")
            lines.append("")
            continue

        lines.append(
            "| 구간 | n | 적중률 | 평균수익 | 중앙값 | 최악 | 사건수 | 사건적중률 | "
            "기준률 | 95%CI | p값 | q값 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for horizon in HORIZONS:
            for window, wlabel in WINDOWS:
                perf = perf_by_signal[sid][horizon][window]
                lines.append(_row_md(f"{horizon}거래일·{wlabel}", perf))
        lines.append("")

    # KHJ_3DAY_SELLOFF 민감도(사용자 요청: -2/-3/-5% 비교)
    if not close.empty:
        lines.append("## KHJ_3DAY_SELLOFF 임계값 민감도 (day_pct, two_day_pct는 day_pct*8/3 비례)")
        lines.append("")
        lines.append("| day_pct | n | 적중률(20거래일) | 평균수익 | 기준률 | 95%CI |")
        lines.append("|---|---|---|---|---|---|")
        for day_pct in (-2.0, -3.0, -5.0):
            two_day_pct = day_pct * 8 / 3  # 기본 -3/-8 비율을 유지한 채 스케일
            levels = sig.khj_3day_selloff(close, day_pct=day_pct, two_day_pct=two_day_pct)
            perf = sb.evaluate_signal(levels["is_on"], close, direction="opportunity", horizons=(PRIMARY_HORIZON,))
            full = perf[PRIMARY_HORIZON]["full"]
            lines.append(
                f"| {day_pct:.0f}% | {full.n} | {_fmt(full.hit_rate_pct)}% | {_fmt(full.mean_return_pct)}% | "
                f"{_fmt_pct(full.base_rate_pct)} | {_fmt_pct(full.ci_lower_pct)}~{_fmt_pct(full.ci_upper_pct)} |"
            )
        lines.append("")

    lines.append(f"\n{DISCLAIMER}")

    reports_dir = Path(__file__).resolve().parent.parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    out_path = reports_dir / "signal_scorecard.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")

    top5 = sorted(top_n_candidates, key=lambda t: -t[1])[:5]
    print(f"저장됨: {out_path}")
    print(f"\n표본 많은 신호 상위 5개({PRIMARY_HORIZON}거래일 기준):")
    for sid, n in top5:
        print(f"  {sid:24s} n={n}")
    print(f"\n표본 부족(n<30) 신호 {len(low_sample_signals)}개:")
    for s in low_sample_signals:
        print(f"  {s}")

    sig_count = sum(
        1 for sid in perf_by_signal if perf_by_signal[sid][PRIMARY_HORIZON]["full"].significant
    )
    print(f"\n통계적으로 유의한(기준률 대비, {PRIMARY_HORIZON}거래일·전체 기간 기준) 신호: {sig_count}개")


if __name__ == "__main__":
    main()
