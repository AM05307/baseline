"""
단계 내 성과 계산 — executor.db 스냅샷·주문·사건으로 TierMetrics를 만든다.

단순 보유 비교에는 baseline의 시세(db.store.get_prices)를 **읽기만** 한다. 값이 없으면 None으로 둔다.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from executor import store
from executor.tiers import TierMetrics

BENCHMARK_TICKER = "000660"
REFERENCE_ACCOUNT = "shadow"  # 항상 돌아가는 섀도 계좌. 섀도 자신의 비교 기준은 스냅샷 reference_value(백테스트)


def _mdd_pct(values: pd.Series) -> float | None:
    v = pd.Series(values, dtype=float).dropna()
    if len(v) < 2:
        return None
    return float((v / v.cummax() - 1).min() * 100)


def _cum_pct(values: pd.Series) -> float | None:
    v = pd.Series(values, dtype=float).dropna()
    if len(v) < 2 or v.iloc[0] <= 0:
        return None
    return float((v.iloc[-1] / v.iloc[0] - 1) * 100)


def compute_metrics(
    snaps: pd.DataFrame,
    orders_df: pd.DataFrame,
    events_df: pd.DataFrame,
    entry_date: str | None,
    end_date: str | None = None,
    reference: pd.Series | None = None,
    benchmark_close: pd.Series | dict[str, pd.Series | None] | None = None,
) -> TierMetrics:
    """
    snaps: 이 계좌의 스냅샷(date, sleeve_value, reference_value). [entry_date, end_date] 구간만 쓴다 —
      종료일을 포함한다: 강등된 날의 손실은 강등 전 단계에서 난 것이라 그 단계 성과에 들어가야 한다.
    reference: 스냅샷의 reference_value가 비어 있을 때 쓸 비교 기준 자산 Series(index=date 문자열) —
      실제 계좌라면 같은 날짜의 가상 계좌(섀도) 슬리브.
    """
    def _window(df: pd.DataFrame, col: str) -> pd.DataFrame:
        if df.empty:
            return df
        out = df[df[col] >= entry_date] if entry_date else df
        return out[out[col] <= end_date] if end_date else out

    s = _window(snaps, "date")
    o = _window(orders_df, "decision_date")
    e = _window(events_df, "date")
    notes = []
    sleeve = s.set_index("date")["sleeve_value"] if not s.empty else pd.Series(dtype=float)

    divergence = None
    ref = s.set_index("date")["reference_value"] if not s.empty else pd.Series(dtype=float)
    if ref.isna().all() and reference is not None:
        ref = pd.Series(reference).reindex(sleeve.index)
    both = pd.DataFrame({"a": sleeve, "r": ref}).dropna()
    a_cum, r_cum = _cum_pct(both["a"]), _cum_pct(both["r"])
    if a_cum is not None and r_cum is not None:
        divergence = abs(a_cum - r_cum)
    elif len(sleeve) >= 2:
        notes.append("비교 기준 없음 — 괴리 계산 불가")

    if isinstance(benchmark_close, pd.Series):
        benchmark_close = {"benchmark": benchmark_close}
    benchmarks: dict[str, dict] = {}
    for name, series in (benchmark_close or {}).items():
        ret = mdd = None
        if series is not None and len(sleeve) >= 2:
            bc = pd.Series(series).sort_index()
            bc.index = pd.to_datetime(bc.index)
            bc = bc[(bc.index >= pd.Timestamp(sleeve.index[0])) & (bc.index <= pd.Timestamp(sleeve.index[-1]))]
            ret, mdd = _cum_pct(bc), _mdd_pct(bc)
        if ret is None and len(sleeve) >= 2:
            notes.append(f"단순 보유 비교 시세 없음: {name}")
        benchmarks[name] = {"return_pct": ret, "mdd_pct": mdd}
    first = next(iter(benchmarks.values()), {"return_pct": None, "mdd_pct": None})
    bh_ret, bh_mdd = first["return_pct"], first["mdd_pct"]

    trades = int((o["filled_qty"] > 0).sum()) if not o.empty else 0
    errors = int(e["kind"].isin(store.ERROR_KINDS).sum()) if not e.empty else 0
    mismatches = int(e["kind"].isin(store.MISMATCH_KINDS).sum()) if not e.empty else 0
    return TierMetrics(
        entry_date=entry_date, last_date=str(sleeve.index[-1]) if len(sleeve) else None,
        days=len(sleeve), trades=trades, cum_return_pct=_cum_pct(sleeve), mdd_pct=_mdd_pct(sleeve),
        divergence_pct=divergence, order_errors=errors, reconciliation_mismatches=mismatches,
        bh_return_pct=bh_ret, bh_mdd_pct=bh_mdd, benchmarks=benchmarks, notes=notes,
    )


def baseline_benchmark(ticker: str = BENCHMARK_TICKER) -> pd.Series | None:
    """baseline.db 시세를 읽기만 한다. 없거나 읽을 수 없으면 None."""
    try:
        from db import store as baseline_store  # 읽기 전용 import (의존 방향 테스트 참고)
        s = baseline_store.get_prices(ticker)
    except Exception:
        return None
    return s if not s.empty else None


def baseline_benchmarks(tickers) -> dict[str, pd.Series | None]:
    """설정의 비교 종목 전부. 시세가 없는 종목도 키는 남긴다(화면·기준에 "데이터 없음"으로 보이게)."""
    return {t: baseline_benchmark(t) for t in tickers}


def tier_metrics_from_db(account: str, entry_date: str | None, end_date: str | None = None,
                         benchmark_close: pd.Series | None = None, path: Path | None = None) -> TierMetrics:
    snaps = store.snapshots(account, path=path)
    reference = None
    if account != REFERENCE_ACCOUNT:  # 섀도가 아닌 계좌의 비교 기준 = 같은 날짜의 섀도 운용분
        v = store.snapshots(REFERENCE_ACCOUNT, path=path)
        if not v.empty:
            reference = v.set_index("date")["sleeve_value"]
    return compute_metrics(snaps, store.orders(account, path=path), store.events(account, path=path),
                           entry_date, end_date, reference, benchmark_close)


def baseline_latest_prices(tickers) -> dict[str, float]:
    """baseline.db의 최신 종가(읽기 전용). 없는 종목은 빠진다 — 호출부가 누락을 처리한다."""
    out: dict[str, float] = {}
    try:
        from db import store as baseline_store
    except Exception:
        return out
    for t in tickers:
        s = baseline_store.get_prices(t)
        if not s.empty:
            out[t] = float(s.sort_index().iloc[-1])
    return out
