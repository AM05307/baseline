"""
신호 가중치를 "특정 시점 t까지의 데이터만으로" 계산하는 순수 함수와, 월 단위
백필 시점 격자.

scripts/recompute_weights.py(실시간)와 scripts/backfill_weights.py(과거 백필)가
**같은 함수(compute_weights_as_of)**를 쓴다 — 두 경로의 계산이 갈라지지 않게 하려는
것이다. 차이는 `as_of`뿐이다: 실시간은 None(전체 데이터), 백필은 과거 시점 t.

**미래참조 방지**: as_of가 주어지면 시세(close)와 신호값(is_on) **둘 다** 그 날짜
이하로 자른 뒤 평가한다. 평가 함수(analysis/signal_backtest.evaluate_signal)는 "마지막
관측일 기준 최근 1년"을 out-of-sample로 갈라 가중치에는 in-sample만 쓰고, 20거래일
선행수익률은 자른 시세 안에서만 계산되므로(끝에서 20일은 NaN → 표본 제외) t 이후
데이터는 어떤 경로로도 들어오지 않는다. tests/test_weight_backfill.py가 t 이후
데이터를 바꿔도 t 시점 가중치가 안 변하는지 확인한다.

가정(숨기지 않음): signal_values는 각 날짜에 "그 날 알 수 있던 값"으로 저장돼 있다고
본다(analysis/signals.py 함수들은 과거만 보는 rolling/shift만 쓰고 shift(-n)이 없다).
수동 입력 의존 신호는 effective_on 기준 forward-fill이다. enabled/weight_cap_pct 같은
신호 메타는 이력이 없어 **현재 값을 과거에도 그대로 썼다고 가정**하는 근사다.

이 모듈은 analysis/ 관례대로 DB/API를 import하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from analysis import signal_backtest as sb
from analysis import weighting as wt

PRIMARY_HORIZON = sb.PRIMARY_HORIZON  # 20거래일


@dataclass
class WeightSnapshot:
    as_of: pd.Timestamp | None                   # 실제로 쓴 마지막 시세일(None이면 시세가 비어 있었음)
    weights: dict[str, float]
    in_sample: dict[str, sb.SignalPerformance]
    out_of_sample: dict[str, sb.SignalPerformance]
    expert_of: dict[str, str]
    caps: dict[str, float]
    signal_ids: list[str]
    n_close: int = 0
    evidence_signals: int = 0                    # 원점수 > 0 (신뢰구간 하한 > 기준률 & 이벤트 수 충분)인 신호 수
    min_events: int = wt.DEFAULT_MIN_EVENTS_FOR_WEIGHT

    @property
    def evidence_based(self) -> bool:
        """False면 모든 신호의 원점수가 0이라 균등 가중치 초기값 규칙이 적용된 스냅샷이다."""
        return self.evidence_signals > 0


def _empty_perf() -> sb.SignalPerformance:
    return sb.SignalPerformance(
        direction="risk", horizon=PRIMARY_HORIZON, n=0, hit_rate_pct=float("nan"),
        mean_return_pct=float("nan"), median_return_pct=float("nan"), worst_pct=float("nan"),
        reliable=False, n_events=0, hit_rate_pct_events=float("nan"),
    )


def compute_weights_as_of(
    close: pd.Series,
    signals: list[dict],
    is_on_by_signal: dict[str, pd.Series],
    as_of: pd.Timestamp | None = None,
    min_events: int = wt.DEFAULT_MIN_EVENTS_FOR_WEIGHT,
    horizon: int = PRIMARY_HORIZON,
) -> WeightSnapshot:
    """
    signals: [{signal_id, direction, expert_id, weight_cap_pct}, ...] — direction이
    'risk'/'opportunity'가 아닌 행(KSJ_DCA 등)은 호출부가 미리 걸러도, 여기서 걸러도 된다.
    is_on_by_signal: signal_id -> is_on Series(index=날짜). 값이 없는 신호는 키가 없어도 된다
    (그 신호는 이벤트 0건 → 가중치 0 후보로 계산에 들어간다).
    """
    close = close.sort_index()
    if as_of is not None:
        close = close[close.index <= as_of]

    expert_of: dict[str, str] = {}
    caps: dict[str, float] = {}
    direction_of: dict[str, str] = {}
    for row in signals:
        if row["direction"] not in ("risk", "opportunity"):
            continue
        sid = row["signal_id"]
        expert_of[sid] = row["expert_id"]
        direction_of[sid] = row["direction"]
        cap = row.get("weight_cap_pct")
        if cap is not None and cap == cap:  # NaN/None 제외 — 과거에 터진 회귀(test_recompute_weights)
            caps[sid] = float(cap)

    signal_ids = list(expert_of.keys())
    in_sample: dict[str, sb.SignalPerformance] = {}
    oos: dict[str, sb.SignalPerformance] = {}

    if not close.empty:
        for sid in signal_ids:
            values = is_on_by_signal.get(sid)
            if values is None or values.empty:
                continue
            is_on = values[values.index <= close.index[-1]]
            perf = sb.evaluate_signal(
                is_on, close, direction=direction_of[sid], horizons=(horizon,), min_events=min_events,
            )
            in_sample[sid] = perf[horizon]["in_sample"]
            oos[sid] = perf[horizon]["out_of_sample"]

    # 다중비교 보정(BH q-value)은 가중치에 안 쓴다(표시용) — 실시간 스크립트와 동일.
    sb.apply_benjamini_hochberg(list(in_sample.values()))

    perf_for_weighting = {}
    for sid in signal_ids:
        p = in_sample.get(sid) or _empty_perf()
        perf_for_weighting[sid] = p
    weights = wt.compute_weights_significant(
        perf_for_weighting, expert_of, signal_caps_pct=caps, min_events=min_events,
    )
    evidence = sum(
        1 for sid, p in perf_for_weighting.items()
        if wt.raw_score_significant(p.edge_pct, p.ci_lower_pct, p.base_rate_pct, p.n_events, min_events) > 0
    )
    return WeightSnapshot(
        as_of=close.index[-1] if not close.empty else None, weights=weights,
        in_sample=in_sample, out_of_sample=oos, expert_of=expert_of, caps=caps,
        signal_ids=signal_ids, n_close=len(close), evidence_signals=evidence, min_events=min_events,
    )


def month_end_grid(
    trading_days: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp, every_months: int = 1,
) -> list[pd.Timestamp]:
    """
    [start, end] 안에서 매 every_months개월마다 "그 달의 마지막 거래일"을 고른다.
    달력상 월말이 아니라 **실제 거래일**이라야 그 날 종가까지의 데이터로 계산했다는
    의미가 분명하다. 정렬된 오름차순 리스트.
    """
    if every_months < 1:
        raise ValueError("every_months must be >= 1")
    days = pd.DatetimeIndex(trading_days).sort_values()
    days = days[(days >= start) & (days <= end)]
    if days.empty:
        return []
    s = pd.Series(days, index=days)
    last_per_month = s.groupby([days.year, days.month]).max().sort_values()
    picked = list(last_per_month.iloc[::every_months])
    return [pd.Timestamp(d) for d in picked]
