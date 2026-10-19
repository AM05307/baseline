"""
weight_history 과거 백필 — 매월 말 거래일마다 "그 시점까지의 데이터만으로" 신호 가중치를
다시 계산해서 weight_history에 source='backfill'로 쌓는다.

왜 하는가: scripts/recompute_weights.py는 아직 한 번만 실제로 돌았다. 그래서
analysis/risk_score_history가 과거 위험 점수를 재구성할 때 전 구간이 "냉시작 균등
가중치" 근사로 채워진다(scripts/risk_score_history_report.py 출력 첫 줄). 이 스크립트는
각 시점에 실시간 재계산을 돌렸다면 나왔을 가중치를 같은 함수(analysis/weight_backfill.
compute_weights_as_of)로 재구성해서 그 구간을 채운다.

**백필이 새 정보를 주는지는 별개다.** 어떤 시점에도 유의한 신호(신뢰구간 하한 > 기준률,
이벤트 ≥ min_events)가 없으면 가중치는 "균등 분배 초기값 규칙"으로 떨어진다 — 이건
냉시작 근사와 같은 숫자이고 이름만 '백필'이 된 것이다. 그래서 리포트가 스냅샷을
"데이터 근거 있음 / 균등 초기값 규칙"으로 나눠서 따로 센다(reason의 evidence_signals).

규칙:
  - **append-only·멱등**: (recomputed_on, signal_id, algorithm_version, source='backfill')가
    이미 있으면 건너뛴다. 산정 방식 버전(db/store.py CURRENT_WEIGHT_ALGORITHM_VERSION)이 바뀌면
    같은 날짜에 새 버전 행이 **추가**되고, 읽는 쪽은 큰 버전을 쓴다. 기존 행(live 포함)은 절대 수정·삭제하지 않는다. 다시 실행해도 같은 결과.
    파라미터(min_events 등)를 바꿔 다시 돌려도 **이미 있는 날짜는 안 바뀐다**는 점에 주의.
  - **미래참조 없음**: 시점 t의 계산은 시세·신호값을 t 이하로 자른 뒤에만 한다.
  - **실시간 행과 충돌 금지**: 백필 구간은 가장 이른 live 행 날짜보다 **이전**으로 제한한다
    (db.store.get_current_weights가 "가장 최근 recomputed_on"을 쓰므로, 그 뒤 날짜의 백필
    행이 '현재 가중치'를 가로채지 못하게).
  - 신호 메타(enabled·weight_cap_pct)는 이력이 없어 현재 값을 과거에도 썼다고 가정한다.

사용:
    python scripts/backfill_weights.py                  # 월 1회, 시세 3년 이상 쌓인 시점부터
    python scripts/backfill_weights.py --dry-run        # DB에 안 쓰고 요약만
    python scripts/backfill_weights.py --every-months 3 --start 2012-01-01
    python scripts/backfill_weights.py --no-report      # 위험 점수 재생성 리포트 생략
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import risk_score_history as rsh  # noqa: E402
from analysis import signal_alignment as sa  # noqa: E402
from analysis import weight_backfill as wb  # noqa: E402
from analysis import weighting as wt  # noqa: E402
from db import store  # noqa: E402

HYNIX = "000660"
DEFAULT_EVERY_MONTHS = 1
DEFAULT_MIN_HISTORY_YEARS = 3  # 시세가 이만큼 쌓이기 전 시점은 in-sample 구간 자체가 너무 짧다(설정값, 백테스트로 고른 값 아님)
_EVIDENCE_RE = re.compile(r"evidence_signals=(\d+)/(\d+)")


def _reason(snapshot: wb.WeightSnapshot, as_of: pd.Timestamp) -> str:
    return (
        f"backfill as_of={as_of.date()} n_close={snapshot.n_close} "
        f"evidence_signals={snapshot.evidence_signals}/{len(snapshot.signal_ids)} "
        f"min_events={snapshot.min_events}"
    )


def evidence_flag(reason: str | None) -> bool | None:
    """reason 문자열의 evidence_signals=K/N에서 K>0 여부. 파싱 불가(live 행 등)면 None."""
    m = _EVIDENCE_RE.search(reason or "")
    return None if not m else int(m.group(1)) > 0


def plan_backfill(
    close: pd.Series,
    signals: list[dict],
    is_on_by_signal: dict[str, pd.Series],
    grid: list[pd.Timestamp],
    existing_by_date: dict[str, dict[str, float]],
    min_events: int,
    progress=None,
) -> tuple[list[dict], list[dict]]:
    """
    grid의 각 날짜에 대해 삽입할 행을 만든다. 반환: (rows, snapshots).
    rows는 store.insert_weight_history 입력(source='backfill'), snapshots는 날짜별 요약
    [{date, evidence_based, computed}] — 이미 DB에 있어서 건너뛴 날짜는 computed=False이고
    evidence_based는 저장된 reason에서 읽은 값(없으면 None).
    """
    directional_ids = [s["signal_id"] for s in signals if s["direction"] in ("risk", "opportunity")]
    rows: list[dict] = []
    snapshots: list[dict] = []
    prev: dict[str, float] = {}
    for i, as_of in enumerate(grid):
        key = as_of.date().isoformat()
        stored = existing_by_date.get(key)
        if stored and all(sid in stored["weights"] for sid in directional_ids):
            prev = dict(stored["weights"])  # 이미 있는 날짜 — 계산·삽입 모두 생략, 다음 old_weight 체인만 잇는다
            snapshots.append({"date": key, "evidence_based": stored["evidence_based"], "computed": False})
            continue
        snap = wb.compute_weights_as_of(close, signals, is_on_by_signal, as_of=as_of, min_events=min_events)
        reason = _reason(snap, as_of)
        for sid in snap.signal_ids:
            rows.append({
                "recomputed_on": key, "signal_id": sid, "old_weight_pct": prev.get(sid),
                "new_weight_pct": snap.weights[sid], "reason": reason, "source": "backfill",
            })
        prev = dict(snap.weights)
        snapshots.append({"date": key, "evidence_based": snap.evidence_based, "computed": True})
        if progress and (i + 1) % 24 == 0:
            progress(i + 1, len(grid))
    return rows, snapshots


def cold_start_days(
    weight_history: pd.DataFrame, trading_days: pd.DatetimeIndex, signal_ids: list[str],
) -> int:
    """weight_history(recomputed_on, signal_id, new_weight_pct)로 본 냉시작(가중치 기록 없는) 거래일 수."""
    wh = weight_history
    if not wh.empty:
        wh = wh.copy()
        wh["recomputed_on"] = pd.to_datetime(wh["recomputed_on"])
        # 빈 DataFrame과 concat하면 object dtype이 되어 weights_as_of의 float 컬럼 대입이 깨진다.
        wh["new_weight_pct"] = pd.to_numeric(wh["new_weight_pct"], errors="coerce")
    _, cold = rsh.effective_weights_as_of(wh, trading_days, signal_ids)
    return int(cold.sum())


def days_under_snapshots(snapshots: list[dict], trading_days: pd.DatetimeIndex) -> dict[str, int]:
    """
    각 거래일에 적용되는(그 날 이전 가장 최근) 백필 스냅샷 종류별 일수:
    evidence(데이터 근거) / fallback(균등 초기값 규칙) / unknown(근거 여부 모름) / none(스냅샷 이전).
    """
    out = {"evidence": 0, "fallback": 0, "unknown": 0, "none": 0}
    if not snapshots:
        out["none"] = len(trading_days)
        return out
    snap_df = pd.DataFrame({
        "date": pd.to_datetime([s["date"] for s in snapshots]),
        "flag": [s["evidence_based"] for s in snapshots],
    }).sort_values("date")
    days_df = pd.DataFrame({"date": pd.DatetimeIndex(trading_days).sort_values()})
    first = snap_df["date"].iloc[0]
    out["none"] = int((days_df["date"] < first).sum())
    merged = pd.merge_asof(days_df[days_df["date"] >= first], snap_df, on="date", direction="backward")
    flags = merged["flag"].tolist()
    out["evidence"] = sum(1 for f in flags if f is True)
    out["fallback"] = sum(1 for f in flags if f is False)
    out["unknown"] = len(flags) - out["evidence"] - out["fallback"]
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="weight_history 백필 (월 1회 시점별 재계산, source='backfill')")
    p.add_argument("--ticker", default=HYNIX)
    p.add_argument("--every-months", type=int, default=DEFAULT_EVERY_MONTHS, help="몇 개월마다 한 번 (기본 1)")
    p.add_argument("--start", help="백필 시작일 YYYY-MM-DD (기본: 시세 시작 + --min-history-years)")
    p.add_argument("--end", help="백필 종료일 YYYY-MM-DD (기본: 마지막 시세일, 가장 이른 live 행 이전으로 제한)")
    p.add_argument("--min-history-years", type=int, default=DEFAULT_MIN_HISTORY_YEARS)
    p.add_argument("--min-events", type=int, default=wt.DEFAULT_MIN_EVENTS_FOR_WEIGHT)
    p.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 요약만")
    p.add_argument("--no-report", action="store_true", help="백필 후 위험 점수 재구성 리포트 생략")
    args = p.parse_args()

    store.init_db()
    close = store.get_prices(args.ticker).sort_index()
    if close.empty:
        print(f"[{args.ticker}] 시세가 없습니다. 먼저 수집하세요.")
        sys.exit(1)
    signals_df = store.get_signals(enabled_only=True)
    signals = signals_df.to_dict("records")
    directional_ids = [s["signal_id"] for s in signals if s["direction"] in ("risk", "opportunity")]
    if not directional_ids:
        print("켜고 끄는(risk/opportunity) 신호가 없습니다. scripts/seed_signals.py 먼저 실행하세요.")
        sys.exit(1)

    is_on_by_signal = {}
    for sid in directional_ids:
        values = store.get_signal_values(sid)
        # 해외 세션 신호는 한국 t일 결정에 t일 이전 값만, 원천 휴장일은 직전 값 유지(analysis/signal_alignment.py)
        if not values.empty:
            is_on_by_signal[sid] = sa.aligned_is_on(sid, values, close.index)

    wh_all = store.get_weight_history()
    if "source" not in wh_all.columns:
        wh_all["source"] = "live"
    live = wh_all[wh_all["source"] != "backfill"]
    if "algorithm_version" not in wh_all.columns:
        wh_all["algorithm_version"] = None
    # "이미 백필됨"은 현재 산정 방식 버전의 행만 센다 — 옛 버전 행은 그대로 두고 새 버전을 추가한다.
    backfilled = wh_all[(wh_all["source"] == "backfill")
                        & (wh_all["algorithm_version"] == store.CURRENT_WEIGHT_ALGORITHM_VERSION)]

    start = pd.Timestamp(args.start) if args.start else close.index[0] + pd.DateOffset(years=args.min_history_years)
    end = pd.Timestamp(args.end) if args.end else close.index[-1]
    if not live.empty:
        first_live = pd.to_datetime(live["recomputed_on"]).min()
        end = min(end, first_live - pd.Timedelta(days=1))
    grid = wb.month_end_grid(close.index, start, end, every_months=args.every_months)
    if not grid:
        print(f"백필할 시점이 없습니다 (start={start.date()}, end={end.date()}).")
        sys.exit(0)

    existing_by_date: dict[str, dict] = {}
    for date_key, grp in backfilled.groupby("recomputed_on"):
        flags = [evidence_flag(r) for r in grp["reason"]]
        existing_by_date[date_key] = {
            "weights": dict(zip(grp["signal_id"], grp["new_weight_pct"])),
            "evidence_based": next((f for f in flags if f is not None), None),
        }

    print(f"[{args.ticker}] 백필 격자 {len(grid)}개 시점 ({grid[0].date()} ~ {grid[-1].date()}, "
          f"{args.every_months}개월 간격) · min_events={args.min_events}")
    print(f"산정 방식 버전 {store.CURRENT_WEIGHT_ALGORITHM_VERSION} · "
          f"이 버전으로 이미 백필된 시점 {sum(1 for d in grid if d.date().isoformat() in existing_by_date)}개는 건너뜁니다.")

    rows, snapshots = plan_backfill(
        close, signals, is_on_by_signal, grid, existing_by_date, args.min_events,
        progress=lambda done, total: print(f"  ... {done}/{total} 시점 계산"),
    )

    # --- 냉시작 구간 변화 (백필 전 → 후) ---
    sids_for_cold = directional_ids
    before_cold = cold_start_days(wh_all, close.index, sids_for_cold)
    parts = [df for df in (wh_all, pd.DataFrame(rows)) if not df.empty]
    after_wh = pd.concat(parts, ignore_index=True) if parts else wh_all
    after_cold = cold_start_days(after_wh, close.index, sids_for_cold)
    total_days = len(close)

    n_new = len({r["recomputed_on"] for r in rows})
    n_ev = sum(1 for s in snapshots if s["evidence_based"] is True)
    n_fb = sum(1 for s in snapshots if s["evidence_based"] is False)
    n_unknown = sum(1 for s in snapshots if s["evidence_based"] is None)
    under = days_under_snapshots(snapshots, close.index)

    print("\n=== 백필 요약 ===")
    print(f"새로 계산한 시점 {n_new}개 · 삽입 대상 행 {len(rows)}개 (이미 있던 시점은 변경 없음)")
    print(f"스냅샷 {len(snapshots)}개 중 — 데이터 근거 있음 {n_ev}개 · 균등 초기값 규칙 {n_fb}개"
          + (f" · 판별 불가 {n_unknown}개" if n_unknown else ""))
    print(f"냉시작(가중치 기록이 없는) 거래일: {before_cold}일({before_cold / total_days * 100:.1f}%)"
          f" → {after_cold}일({after_cold / total_days * 100:.1f}%)  [전체 {total_days}거래일]")
    print(f"백필 이후 구간의 적용 스냅샷별 거래일: 데이터 근거 {under['evidence']}일 · "
          f"균등 초기값 규칙 {under['fallback']}일" + (f" · 판별 불가 {under['unknown']}일" if under["unknown"] else ""))
    if n_ev == 0:
        print("⚠ 어느 시점에도 통계적으로 유의한 신호가 없었습니다 — 냉시작 구간이 줄어든 것은 '기록'이 채워진 것이고, "
              "가중치 값은 균등 초기값 규칙이라 냉시작 근사와 같은 숫자입니다(새로운 정보 아님).")

    if args.dry_run:
        print("\n--dry-run: DB에 기록하지 않았습니다.")
        return

    inserted = store.insert_weight_history(rows)
    print(f"\nweight_history에 백필 {inserted}행 기록됨 (source='backfill').")

    if not args.no_report and inserted:
        print("\n=== 위험 점수 재구성 (백필 반영) ===")
        from scripts import risk_score_history_report
        risk_score_history_report.run(args.ticker)


if __name__ == "__main__":
    main()
