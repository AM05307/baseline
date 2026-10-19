"""
급변일 설명 카드 — "같은 날 함께 움직인 지표"를 나란히 놓는다(관계 측정, 예측 아님).

종목의 일간 수익률이 임계값(기본 ±3%)을 넘은 날을 찾고, 그날의 주변 지표
변화를 "평소 베타 × 그날 변화량" = 기대 기여분으로 환산한다. 기여분을 뺀
나머지는 **잔차**로 따로 둔다 — 항등식 `기여분 합 + 잔차 = 실제 수익률`이
항상 성립한다(tests/test_move_attribution.py가 검증).

**인과를 단정하지 않는다.** 이 모듈이 내는 숫자는 "평소 관계가 그날 얼마나
설명하는가"일 뿐, "~ 때문에 떨어졌다"가 아니다. 특히 아래 한계를 결과에
그대로 싣는다(화면에서 숨기지 않는다):
  - 지표별 베타는 단변량 추정이라 SOX·NVDA처럼 서로 겹치는 지표의 기여분을
    더하면 과대 계산될 수 있다(잔차가 음수 쪽으로 밀림).
  - 코스피는 000660 자체를 포함한다(순환적).
  - 베타 추정 구간·표본수는 지표마다 항상 함께 반환한다(원칙 3).

**미래참조 방지**: 베타는 급변일 **이전** 거래일 `beta_window`개로만 추정한다
(급변일 자신은 제외 — 그날 수익률이 자기 베타를 끌어올리는 순환을 막는다).

**시차 정렬**: 미국 지표는 한국 장이 열리기 전에 이미 마감된 "직전 미국
거래일"의 변화를 쓴다(`prior_us_session`, 한국 날짜보다 엄격히 이전인 마지막
거래일). 한국/환율 지표는 같은 날(`same_day`). USDKRW를 same_day로 두는 건
단순화다 — 환율은 24시간 거래라 일 종가 시각이 한국 장 마감과 다를 수 있다.

이 모듈은 analysis/ 관례대로 순수 함수만 담는다(DB/API 비의존). DB에서 읽어서
이 모듈의 입력 모양으로 바꾸는 일은 web/main.py가 한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

DEFAULT_THRESHOLD_PCT = 3.0
DEFAULT_BETA_WINDOW = 60
MAX_SESSION_GAP_DAYS = 5  # 직전 미국 세션이 한국 날짜보다 이보다 오래 전이면 "오래된 값"이라 쓰지 않는다
MIN_SAMPLES = 30  # 원칙 3. 다른 analysis 모듈과 같은 기준값, 독립 상수(기존 관행).

TIMING_SAME_DAY = "same_day"
TIMING_PRIOR_US = "prior_us_session"
KIND_PCT = "pct"     # 변화량 = 일간 수익률(%)
KIND_DIFF = "diff"   # 변화량 = 수준 차이(%p) — 금리처럼 퍼센트 자체가 수준인 지표


@dataclass(frozen=True)
class IndicatorSpec:
    key: str          # DB ticker
    label: str
    timing: str       # TIMING_SAME_DAY | TIMING_PRIOR_US
    kind: str         # KIND_PCT | KIND_DIFF
    note: str | None = None


DEFAULT_INDICATORS: tuple[IndicatorSpec, ...] = (
    IndicatorSpec("SOX", "SOX", TIMING_PRIOR_US, KIND_PCT),
    IndicatorSpec("NVDA", "엔비디아", TIMING_PRIOR_US, KIND_PCT),
    IndicatorSpec("US10Y", "미국 10년물", TIMING_PRIOR_US, KIND_DIFF),
    IndicatorSpec("USDKRW", "원/달러", TIMING_SAME_DAY, KIND_PCT),
    IndicatorSpec("VIX", "VIX", TIMING_PRIOR_US, KIND_PCT),
    IndicatorSpec("KOSPI", "코스피", TIMING_SAME_DAY, KIND_PCT, note="000660 자체를 포함(순환적)"),
)

# 지표 상태 — 값을 못 구한 이유를 숨기지 않고 화면에 그대로 보여주기 위한 라벨.
STATUS_OK = "ok"
STATUS_NO_SERIES = "no_series"          # 해당 지표 시세 자체가 DB에 없음
STATUS_NO_CHANGE = "no_change"          # 그날(또는 직전 미국 거래일) 변화량을 못 구함
STATUS_LOW_SAMPLES = "low_samples"      # 베타 추정 표본이 MIN_SAMPLES 미만 → 기여분에서 제외
STATUS_NO_BETA = "no_beta"              # 베타 계산 불가(표본<2 또는 분산 0)


@dataclass
class IndicatorContribution:
    key: str
    label: str
    timing: str
    kind: str
    status: str
    change: float | None = None          # 그날 변화량(pct 또는 %p)
    change_date: str | None = None       # 변화량이 측정된 지표 쪽 날짜
    beta: float | None = None
    beta_samples: int = 0
    beta_window_start: str | None = None
    beta_window_end: str | None = None
    contribution_pct: float | None = None  # 기대 기여분(%p). status != ok면 None
    note: str | None = None

    @property
    def reliable(self) -> bool:
        return self.beta_samples >= MIN_SAMPLES


@dataclass
class MoveCard:
    date: str
    return_pct: float
    direction: str                       # 'up' | 'down' — 사실 서술용(예측 아님)
    contributions: list[IndicatorContribution]
    explained_pct: float                 # 기여분 합(기여분이 하나도 없으면 0.0)
    residual_pct: float                  # 실제 - 기여분 합
    n_contributing: int
    flow: dict | None = None             # {'individual','foreigner','institution'} 또는 None
    items: list[dict] = field(default_factory=list)  # 그날 뉴스·공시·claim
    threshold_pct: float = DEFAULT_THRESHOLD_PCT
    beta_window: int = DEFAULT_BETA_WINDOW
    target_samples: int = 0              # 임계값 판정에 쓴 일간 수익률 표본 수


def daily_returns_pct(prices: pd.Series) -> pd.Series:
    """종가 → 일간 수익률(%). NaN 제거, 날짜 오름차순."""
    s = prices.dropna().sort_index()
    return (s.pct_change() * 100).dropna()


def find_move_days(prices: pd.Series, threshold_pct: float = DEFAULT_THRESHOLD_PCT) -> pd.Series:
    """일간 수익률(%) 절댓값이 threshold_pct 이상인 날(오름차순)."""
    r = daily_returns_pct(prices)
    return r[r.abs() >= threshold_pct]


def _indicator_changes(prices: pd.Series, kind: str) -> pd.Series:
    s = prices.dropna().sort_index()
    if kind == KIND_DIFF:
        return s.diff().dropna()
    return (s.pct_change() * 100).dropna()


def align_to_kr_dates(
    changes: pd.Series, kr_dates: pd.DatetimeIndex, timing: str,
    max_gap_days: int = MAX_SESSION_GAP_DAYS,
) -> pd.DataFrame:
    """
    지표 변화량을 한국 거래일에 맞춰 정렬한다. 반환 DataFrame 컬럼:
      change      — 그 한국 거래일에 대응하는 지표 변화량(없으면 NaN)
      source_date — 그 변화량이 측정된 지표 쪽 날짜

    same_day:  같은 날짜의 변화량(정확히 일치하는 날만).
    prior_us_session: 한국 날짜보다 **엄격히 이전**인 가장 가까운 지표 날짜의
               변화량(merge_asof, backward, allow_exact_matches=False) —
               미국 장이 한국 장 개장 전에 이미 끝난 날을 가리킨다. 그 날짜가
               한국 날짜보다 max_gap_days일보다 더 오래 전이면(지표 수집이 끊긴
               경우) 오래된 값을 "직전 세션"으로 둔갑시키지 않고 NaN으로 둔다.
    """
    left = pd.DataFrame({"date": pd.DatetimeIndex(kr_dates).sort_values()})
    if changes.empty:
        return pd.DataFrame({"change": float("nan"), "source_date": pd.NaT}, index=left["date"])
    right = changes.rename("change").rename_axis("src").reset_index()
    right["source_date"] = right["src"]
    if timing == TIMING_PRIOR_US:
        merged = pd.merge_asof(
            left, right.rename(columns={"src": "date"}).sort_values("date"),
            on="date", direction="backward", allow_exact_matches=False,
            tolerance=pd.Timedelta(days=max_gap_days),
        )
    else:
        merged = left.merge(right.rename(columns={"src": "date"}), on="date", how="left")
    return merged.set_index("date")[["change", "source_date"]]


def estimate_beta(
    target_ret: pd.Series, indicator_change: pd.Series, before: pd.Timestamp, window: int
) -> tuple[float, int, pd.Timestamp | None, pd.Timestamp | None]:
    """
    before(제외) 이전 거래일 중 target·indicator 둘 다 값이 있는 마지막 window개로
    단변량 베타(cov/var)를 구한다. 반환: (beta 또는 NaN, 표본수, 구간 시작, 구간 끝).
    """
    joined = pd.concat([target_ret.rename("t"), indicator_change.rename("x")], axis=1).dropna()
    joined = joined[joined.index < before].tail(window)
    n = len(joined)
    if n < 2:
        return float("nan"), n, None, None
    var = joined["x"].var()
    if not var or var != var:
        return float("nan"), n, joined.index[0], joined.index[-1]
    beta = float(joined["t"].cov(joined["x"]) / var)
    return beta, n, joined.index[0], joined.index[-1]


def _fmt_date(ts) -> str | None:
    if ts is None or ts is pd.NaT or (isinstance(ts, float) and math.isnan(ts)):
        return None
    return pd.Timestamp(ts).date().isoformat()


def attribute_move(
    day: pd.Timestamp,
    target_ret: pd.Series,
    indicators: dict[str, pd.Series],
    specs: tuple[IndicatorSpec, ...] = DEFAULT_INDICATORS,
    beta_window: int = DEFAULT_BETA_WINDOW,
) -> tuple[list[IndicatorContribution], float, float, int]:
    """
    한 급변일의 지표별 기여분을 계산한다.
    반환: (contributions, explained_pct, residual_pct, n_contributing).
    항등식: explained + residual == target_ret[day] (부동소수 오차 범위).
    """
    actual = float(target_ret.loc[day])
    kr_dates = target_ret.index
    contributions: list[IndicatorContribution] = []
    explained = 0.0
    n_contrib = 0

    for spec in specs:
        base = dict(key=spec.key, label=spec.label, timing=spec.timing, kind=spec.kind, note=spec.note)
        prices = indicators.get(spec.key)
        if prices is None or prices.dropna().empty:
            contributions.append(IndicatorContribution(status=STATUS_NO_SERIES, **base))
            continue

        changes = _indicator_changes(prices, spec.kind)
        aligned = align_to_kr_dates(changes, kr_dates, spec.timing)
        change_today = aligned["change"].get(day, float("nan"))
        src_date = aligned["source_date"].get(day, pd.NaT)

        beta, n, w_start, w_end = estimate_beta(target_ret, aligned["change"], day, beta_window)
        info = dict(
            beta=None if beta != beta else beta, beta_samples=n,
            beta_window_start=_fmt_date(w_start), beta_window_end=_fmt_date(w_end),
        )

        if change_today != change_today:
            contributions.append(IndicatorContribution(status=STATUS_NO_CHANGE, **base, **info))
            continue
        base["change"] = float(change_today)
        base["change_date"] = _fmt_date(src_date)

        if beta != beta:
            contributions.append(IndicatorContribution(status=STATUS_NO_BETA, **base, **info))
            continue
        if n < MIN_SAMPLES:
            # 표본이 모자라면 기여분에 넣지 않는다 — 잔차로 남긴다(지어내지 않음).
            contributions.append(IndicatorContribution(status=STATUS_LOW_SAMPLES, **base, **info))
            continue

        contrib = beta * float(change_today)
        explained += contrib
        n_contrib += 1
        contributions.append(IndicatorContribution(
            status=STATUS_OK, contribution_pct=contrib, **base, **info,
        ))

    return contributions, explained, actual - explained, n_contrib


def _flow_for_day(flow: pd.DataFrame | None, day: pd.Timestamp) -> dict | None:
    """investor_flow의 그날 행. 행이 없으면 None(데이터 없음), 값이 NaN인 칸도 None."""
    if flow is None or flow.empty or "date" not in flow.columns:
        return None
    rows = flow[pd.to_datetime(flow["date"]) == day]
    if rows.empty:
        return None
    row = rows.iloc[-1]
    out = {}
    for col in ("individual", "foreigner", "institution"):
        v = row.get(col)
        out[col] = None if v is None or v != v else float(v)
    return out if any(v is not None for v in out.values()) else None


def recent_move_dates(
    prices: pd.Series, threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    lookback_days: int = 250, max_cards: int = 5,
) -> list[pd.Timestamp]:
    """최근 lookback_days 거래일 안의 급변일, 최신순 max_cards개. build_move_cards와 같은 선정 규칙."""
    ret = daily_returns_pct(prices).tail(lookback_days)
    moves = ret[ret.abs() >= threshold_pct]
    return list(moves.index)[::-1][:max_cards]


def build_move_cards(
    target_prices: pd.Series,
    indicators: dict[str, pd.Series],
    flow: pd.DataFrame | None = None,
    items_by_date: dict[str, list[dict]] | None = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    beta_window: int = DEFAULT_BETA_WINDOW,
    lookback_days: int = 250,
    max_cards: int = 5,
    specs: tuple[IndicatorSpec, ...] = DEFAULT_INDICATORS,
) -> list[MoveCard]:
    """
    최근 lookback_days 거래일 안의 급변일을 최신순으로 max_cards개까지 카드로 만든다.

    items_by_date: {'YYYY-MM-DD': [{kind, text, confidence, source}, ...]} —
    그날의 뉴스·공시·claim. confidence 라벨은 호출부가 채워 넣는다(여기서 지어내지
    않는다). 급변일이 없으면 빈 리스트.
    """
    ret = daily_returns_pct(target_prices)
    if ret.empty:
        return []
    recent = ret.tail(lookback_days)
    items_by_date = items_by_date or {}

    cards: list[MoveCard] = []
    for day in recent_move_dates(target_prices, threshold_pct, lookback_days, max_cards):
        contribs, explained, residual, n_contrib = attribute_move(
            day, ret, indicators, specs=specs, beta_window=beta_window,
        )
        actual = float(ret.loc[day])
        cards.append(MoveCard(
            date=day.date().isoformat(), return_pct=actual,
            direction="up" if actual > 0 else "down",
            contributions=contribs, explained_pct=explained, residual_pct=residual,
            n_contributing=n_contrib, flow=_flow_for_day(flow, day),
            items=list(items_by_date.get(day.date().isoformat(), [])),
            threshold_pct=threshold_pct, beta_window=beta_window, target_samples=len(recent),
        ))
    return cards
