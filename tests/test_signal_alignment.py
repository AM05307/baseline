"""
analysis/signal_alignment.py — 해외 세션 신호 날짜 정렬 회귀 테스트(합성 데이터).

배경(2026-10-18): signal_values는 원천 시장의 날짜로 저장되는데, 미국 t일 종가는 한국
t일 장 마감 뒤에 나온다. 같은 날짜로 맞추면 반나절 미래참조, 없는 날을 0으로 채우면
미국 휴장일마다 신호가 꺼졌다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import signal_alignment as sa  # noqa: E402
from analysis import signal_backtest as sb  # noqa: E402


def _df(dates, is_on):
    dates = pd.DatetimeIndex(dates)
    return pd.DataFrame({"date": dates, "value": np.asarray(is_on, float), "is_on": np.asarray(is_on, int)})


KRX = pd.DatetimeIndex(["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13"])


def test_prior_session_list_covers_foreign_signals():
    assert sa.PRIOR_SESSION_SIGNALS == {
        "YJH_US10Y", "KSJ_US10Y_HIGH", "KHJ_IPO_WEAK", "KHJ_CONCENTRATION", "KHJ_COMBO"}


def test_prior_session_uses_strictly_earlier_and_domestic_uses_same_day():
    us = _df(KRX, [0, 1, 0, 0, 0])
    a = sa.align_values(us, KRX, prior_session=True)
    assert np.isnan(a["is_on"].iloc[0])  # 10/6 이전 미국 값 없음
    assert list(a["is_on"].iloc[1:]) == [0, 1, 0, 0]  # 10/7 값은 10/8 결정부터
    assert a["src_date"].iloc[2] == pd.Timestamp("2026-10-07")
    d = sa.align_values(us, KRX, prior_session=False)
    assert list(d["is_on"]) == [0, 1, 0, 0, 0]


def test_us_holiday_holds_previous_value_instead_of_off():
    # 미국 10/12(월) 휴장(콜럼버스 데이 가정): 10/9(금) 값이 on이면 10/13 결정에도 유지돼야 한다
    us_dates = pd.DatetimeIndex(["2026-10-08", "2026-10-09", "2026-10-13"])
    us = _df(us_dates, [1, 1, 0])
    a = sa.align_values(us, KRX, prior_session=True)
    assert a.loc["2026-10-12", "is_on"] == 1  # 10/9 값
    assert a.loc["2026-10-13", "is_on"] == 1  # 10/12 미국 휴장 → 10/9 값 유지(예전엔 0)


def test_stale_value_beyond_gap_is_missing():
    us = _df(["2026-09-01"], [1])
    a = sa.align_values(us, KRX, prior_session=True)
    assert a["is_on"].isna().all()


def test_future_values_do_not_affect_earlier_decisions():
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2024-01-01", periods=60)
    base = _df(dates, rng.random(60) < 0.5)
    for prior in (True, False):
        ref = sa.align_values(base, dates, prior)["is_on"]
        for cut in (10, 30, 50):
            t = dates[cut]
            mod = base.copy()
            mod.loc[mod["date"] > t, "is_on"] = 1 - mod.loc[mod["date"] > t, "is_on"]
            got = sa.align_values(mod, dates, prior)["is_on"]
            pd.testing.assert_series_equal(ref[: cut + 1], got[: cut + 1])
    # 해외 세션은 t일 당일 값을 바꿔도 t일 결정이 그대로여야 한다
    ref = sa.align_values(base, dates, True)["is_on"]
    mod = base.copy()
    mod.loc[mod["date"] == dates[20], "is_on"] = 1 - mod.loc[mod["date"] == dates[20], "is_on"]
    got = sa.align_values(mod, dates, True)["is_on"]
    assert got[dates[20]] == ref[dates[20]] or np.isnan(ref[dates[20]])
    assert got[dates[21]] != ref[dates[21]]  # 대조군: 다음 결정일에는 반영


def test_aligned_is_on_dispatches_by_signal_id():
    us = _df(KRX, [1, 0, 0, 0, 0])
    assert np.isnan(sa.aligned_is_on("YJH_US10Y", us, KRX).iloc[0])
    assert sa.aligned_is_on("KHJ_OVERHEAT", us, KRX).iloc[0] == 1


def test_evaluate_signal_with_alignment_shifts_us_signal_by_one_decision():
    """신호 평가 경로 회귀: 미국 신호가 켜진 날의 '그 날' 이후 수익률을 쓰지 않는다."""
    dates = pd.bdate_range("2024-01-01", periods=200)
    close = pd.Series(100 + np.arange(200, dtype=float), index=dates)
    close.iloc[101] = 50.0  # 신호 다음 날 급락
    on = np.zeros(200, int)
    on[100] = 1
    vals = _df(dates, on)
    aligned = sa.aligned_is_on("YJH_US10Y", vals, dates)
    assert aligned[dates[100]] == 0 and aligned[dates[101]] == 1
    raw = sb.evaluate_signal(vals.set_index("date")["is_on"], close, "risk", horizons=(5,), min_events=1)
    fixed = sb.evaluate_signal(aligned, close, "risk", horizons=(5,), min_events=1)
    assert raw[5]["full"].n == fixed[5]["full"].n == 1
    assert raw[5]["full"].mean_return_pct != pytest.approx(fixed[5]["full"].mean_return_pct)


def test_mixed_datetime_resolutions_do_not_break_alignment():
    """회귀(2026-10-18): date 객체에서 만든 결정일(초 해상도)과 시세 날짜(us)를 merge_asof하면 pandas 3에서 실패했다."""
    from datetime import date
    from analysis import what_if as wi
    vals = _df(pd.bdate_range("2026-10-01", "2026-10-16"), [1] * 12)
    t = pd.DatetimeIndex([pd.Timestamp(date(2026, 10, 16))])
    assert sa.align_values(vals, t, prior_session=True)["is_on"].iloc[0] == 1
    wh = pd.DataFrame([{"recomputed_on": "2026-09-30", "signal_id": "A", "new_weight_pct": 5.0}])
    assert wi.weights_for_dates(wh, t, ["A"]).iloc[0, 0] == 5.0
