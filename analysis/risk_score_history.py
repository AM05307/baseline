"""
과거 위험 점수 시계열 재구성 — 5단계 요청: "미래 참조 없이 재구성, 점수
구간별로 이후 5/20/63거래일 수익률 분포·표본수·최대손실을 보여준다."

**왜 "그 날짜까지 알 수 있었던 가중치"만 써야 하는가**: 신호 가중치는
scripts/recompute_weights.py가 주기적으로 재계산하고(walk-forward,
db/schema.sql weight_history에 (recomputed_on, signal_id, new_weight_pct)
로 append-only 기록), 그 재계산은 "그 시점까지의" 백테스트 성적만 쓴다
(1단계). 과거 날짜 D의 위험 점수를 지금(가장 최신) 가중치로 다시 계산하면,
D 이후에 쌓인 성적으로 정해진 가중치를 D 시점에 이미 알고 있었던 것처럼
쓰는 미래 참조가 된다 — 그래서 날짜 D의 가중치는 weight_history에서
recomputed_on <= D인 가장 최근 값만 쓴다(merge_asof, "backward").

**가중치 기록이 아예 없는 날짜(냉시작 구간)**: analysis/weighting.py의
초기값 규칙("백테스트 history가 없어 raw가 전부 0일 때는 모든 신호 동일
가중치 — 공식이 자연스럽게 그렇게 된다")을 그대로 적용해 균등 가중치로
채운다. 이건 지어낸 숫자가 아니라 **이 시스템이 실제로 쓰는 냉시작 규칙을
그 기간에도 그대로 적용한 것**이다 — 다만 "enabled" 플래그 자체는
signals 테이블에 이력이 없어(현재 상태만 있음) 지금 활성화된 신호 집합을
과거에도 그대로 썼다고 가정하는 근사다(아래 WeightRegime.cold_start=True로
구분해서 표시, 숨기지 않음).

이 모듈은 analysis/ 관례대로 순수 함수만 담는다(DB/API 비의존) — DB에서
weight_history·signal_values를 읽어 이 모듈의 입력 모양으로 바꾸는 건
scripts/risk_score_history_report.py가 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

DEFAULT_SCORE_BUCKETS: list[tuple[float, float]] = [(0, 40), (40, 60), (60, 80), (80, 101)]
MIN_SAMPLES_FOR_BUCKET = 30  # 원칙 3과 같은 기준


def select_weight_rows(weight_history: pd.DataFrame) -> pd.DataFrame:
    """
    같은 recomputed_on·같은 signal_id에 행이 여러 개일 때 하나만 남긴다: algorithm_version이 큰
    행, 그다음 id가 큰 행(db/store.py WEIGHT_ALGORITHM_VERSIONS 주석과 같은 규칙). 컬럼이 없으면
    그 기준은 건너뛴다. 반환은 recomputed_on 오름차순.
    """
    if weight_history.empty:
        return weight_history
    order = ["recomputed_on"] + [c for c in ("algorithm_version", "id") if c in weight_history.columns]
    return (weight_history.sort_values(order, kind="stable", na_position="first")
            .drop_duplicates(["recomputed_on", "signal_id"], keep="last"))


def weights_as_of(
    weight_history: pd.DataFrame, as_of_dates: pd.DatetimeIndex, signal_ids: list[str]
) -> pd.DataFrame:
    """
    weight_history: columns = recomputed_on(datetime64), signal_id, new_weight_pct.
    반환: index=as_of_dates, columns=signal_ids. 각 칸은 "그 날짜 이전(<=)에
    기록된 가장 최근 new_weight_pct" — 그 신호가 아직 한 번도 재계산된 적
    없는 날짜(그 신호의 첫 recomputed_on보다 이전)는 NaN(지어내지 않음,
    호출부가 냉시작 규칙을 적용할지 결정).
    """
    out = pd.DataFrame(index=as_of_dates, columns=signal_ids, dtype=float)
    if weight_history.empty:
        return out
    wh = select_weight_rows(weight_history).copy()
    wh["recomputed_on"] = pd.to_datetime(wh["recomputed_on"]).dt.as_unit("ns")  # merge_asof 해상도 통일(pandas 3)
    dates_df = pd.DataFrame({"date": pd.DatetimeIndex(as_of_dates).as_unit("ns").sort_values()})
    for sid in signal_ids:
        sub = wh[wh["signal_id"] == sid]
        if sub.empty:
            continue
        hist = sub[["recomputed_on", "new_weight_pct"]].rename(
            columns={"recomputed_on": "date", "new_weight_pct": "w"}
        ).sort_values("date")
        merged = pd.merge_asof(dates_df, hist, on="date", direction="backward")
        out.loc[merged["date"].to_numpy(), sid] = merged["w"].to_numpy()
    return out.reindex(as_of_dates)


def effective_weights_as_of(
    weight_history: pd.DataFrame, as_of_dates: pd.DatetimeIndex, enabled_signal_ids: list[str]
) -> tuple[pd.DataFrame, pd.Series]:
    """
    weights_as_of()를 감싸서, 아직 재계산 기록이 없는 날짜(= 해당 신호가
    NaN)는 냉시작 균등 가중치(100/len(enabled_signal_ids))로 채운다.

    반환: (weights DataFrame(as_of_dates x enabled_signal_ids), cold_start
    Series(bool, index=as_of_dates)) — cold_start[d]=True면 그 날짜의
    모든 신호가 냉시작 근사라는 뜻(실제 백테스트로 정해진 가중치가 하나도
    없었던 시기). 신호별로 일부만 냉시작인 경우는 없다고 가정하지 않는다
    — 신호별 cold_start 여부가 섞여도 각 칸은 올바르게 채워진다, 반환하는
    cold_start Series는 "그 날 전체가 냉시작이었는가"(전부 NaN이었는가)만
    표시하는 요약값이다.
    """
    raw = weights_as_of(weight_history, as_of_dates, enabled_signal_ids)
    cold_start = raw.isna().all(axis=1)
    equal_weight = 100.0 / len(enabled_signal_ids) if enabled_signal_ids else 0.0
    filled = raw.fillna(equal_weight)
    return filled, cold_start


def reconstruct_score_series(
    is_on: pd.DataFrame, weights: pd.DataFrame, directions: dict[str, str], net_opportunity: bool = False,
) -> pd.Series:
    """
    날짜별 위험 점수 시계열(0~100) — analysis/risk_score.compute_risk_score()와
    완전히 같은 공식(risk 가중치합 - opportunity 가중치합을 (x+100)/2로
    매핑)을 날짜 전체에 벡터화해서 적용한다. is_on·weights는 index=날짜,
    columns=signal_id로 모양이 같아야 한다(둘 다 없는 신호는 무시).

    KHJ_COMBO 같은 "상태 문구만 바꾸는" 트리거는 점수 자체에 영향을 주지
    않는다(risk_score.determine_status도 동일 — combo는 new_buy_status/
    sell_stage만 바꾸고 risk_score 숫자는 안 바꾼다). 그래서 이 함수는
    점수만 재구성하고 상태 문구는 다루지 않는다.

    net_opportunity=False(기본, 2026-10-18~)면 opportunity 신호는 위험 점수에 안 들어간다
    (analysis/risk_score.py 모듈 docstring 참고). True면 예전 방식(risk − opportunity).
    """
    common_cols = [c for c in is_on.columns if c in weights.columns]
    if not common_cols:
        return pd.Series(50.0, index=is_on.index)  # 신호가 전혀 없으면 중립(50)
    on = is_on[common_cols].fillna(0)
    w = weights[common_cols].fillna(0)
    on_weight = on * w
    risk_cols = [c for c in common_cols if directions.get(c) == "risk"]
    opp_cols = [c for c in common_cols if directions.get(c) == "opportunity"]
    risk_w = on_weight[risk_cols].sum(axis=1) if risk_cols else pd.Series(0.0, index=on.index)
    opp_w = on_weight[opp_cols].sum(axis=1) if opp_cols else pd.Series(0.0, index=on.index)
    raw = risk_w - opp_w if net_opportunity else risk_w
    return ((raw + 100) / 2).clip(lower=0, upper=100)


def reconstruct_opportunity_series(
    is_on: pd.DataFrame, weights: pd.DataFrame, directions: dict[str, str]
) -> pd.Series:
    """날짜별 기회 점수(risk_score.compute_opportunity_score와 같은 공식). 상태 판정에 안 쓴다."""
    opp_cols = [c for c in is_on.columns if c in weights.columns and directions.get(c) == "opportunity"]
    if not opp_cols:
        return pd.Series(50.0, index=is_on.index)
    opp_w = (is_on[opp_cols].fillna(0) * weights[opp_cols].fillna(0)).sum(axis=1)
    return ((opp_w + 100) / 2).clip(lower=0, upper=100)


@dataclass
class ScoreBucketStats:
    bucket_label: str
    horizon: int
    n: int
    mean_return_pct: float
    median_return_pct: float
    worst_return_pct: float  # 최대손실(그 구간 안에서 가장 낮은 수익률)
    reliable: bool  # n >= min_samples (원칙 3)


def _bucket_label(lo: float, hi: float) -> str:
    return f"{lo:.0f}~100" if hi >= 100 else f"{lo:.0f}~{hi:.0f}"


def bucket_forward_returns(
    scores: pd.Series,
    forward_returns_by_horizon: dict[int, pd.Series],
    buckets: list[tuple[float, float]] | None = None,
    min_samples: int = MIN_SAMPLES_FOR_BUCKET,
) -> list[ScoreBucketStats]:
    """
    scores(그 날의 위험 점수, 0~100)와 forward_returns_by_horizon(호라이즌별
    "그 날 이후 N거래일 수익률" Series — 이미 미래 데이터, analysis/
    signal_backtest.forward_returns()로 만든 것을 그대로 받는다, 백테스트
    모듈 예외 그대로 적용)을 받아 점수 구간×호라이즌별 n·평균·중앙값·
    최악(최대손실) 수익률을 낸다.
    """
    buckets = buckets if buckets is not None else DEFAULT_SCORE_BUCKETS
    results: list[ScoreBucketStats] = []
    for horizon, fwd in forward_returns_by_horizon.items():
        df = pd.DataFrame({"score": scores, "ret": fwd}).dropna()
        for lo, hi in buckets:
            mask = (df["score"] >= lo) & (df["score"] < hi)
            sub = df.loc[mask, "ret"]
            n = len(sub)
            results.append(ScoreBucketStats(
                bucket_label=_bucket_label(lo, hi), horizon=horizon, n=n,
                mean_return_pct=float(sub.mean()) if n else float("nan"),
                median_return_pct=float(sub.median()) if n else float("nan"),
                worst_return_pct=float(sub.min()) if n else float("nan"),
                reliable=n >= min_samples,
            ))
    return results


@dataclass
class GroupForwardStats:
    group: str
    horizon: int
    n_events: int  # 연속으로 켜진 구간의 첫날 수(겹치는 표본을 줄인 단위) — 통계는 이 단위로 낸다
    n_days: int  # 켜진 날 전체 수(참고용, 서로 겹침)
    mean_return_pct: float
    median_return_pct: float
    worst_return_pct: float  # 최대손실
    positive_pct: float  # 이후 수익률 > 0인 이벤트 비율
    group_all_days_mean_pct: float  # 같은 그룹(상태)의 모든 날 평균 — 신호 없이도 나오는 기준
    group_all_days_n: int
    reliable: bool


def forward_stats_by_group(
    on: pd.Series,
    group: pd.Series,
    forward_returns_by_horizon: dict[int, pd.Series],
    groups: list[str] | None = None,
    min_samples: int = MIN_SAMPLES_FOR_BUCKET,
) -> list[GroupForwardStats]:
    """
    on(신호 켜짐 bool, index=날짜)을 그 날의 group(예: 위험 상태 '정상'/'1단계')으로 나눠, 이후
    N거래일 수익률을 이벤트 단위(연속 on 구간의 첫날)로 집계한다. 이벤트의 그룹은 그 첫날의
    그룹이다. 비교 기준으로 같은 그룹 모든 날의 평균 수익률도 같이 낸다(원칙 3·4: n·최대손실 동반).
    """
    on = on.fillna(False).astype(bool)
    starts = on & ~on.shift(1, fill_value=False)
    groups = groups if groups is not None else sorted(group.dropna().unique())
    out: list[GroupForwardStats] = []
    for horizon, fwd in forward_returns_by_horizon.items():
        df = pd.DataFrame({"on": on, "start": starts, "g": group, "ret": fwd}).dropna(subset=["ret", "g"])
        for g in groups:
            in_g = df[df["g"] == g]
            ev = in_g.loc[in_g["start"], "ret"]
            n = len(ev)
            out.append(GroupForwardStats(
                group=g, horizon=horizon, n_events=n, n_days=int(in_g["on"].sum()),
                mean_return_pct=float(ev.mean()) if n else float("nan"),
                median_return_pct=float(ev.median()) if n else float("nan"),
                worst_return_pct=float(ev.min()) if n else float("nan"),
                positive_pct=float((ev > 0).mean() * 100) if n else float("nan"),
                group_all_days_mean_pct=float(in_g["ret"].mean()) if len(in_g) else float("nan"),
                group_all_days_n=len(in_g),
                reliable=n >= min_samples,
            ))
    return out
