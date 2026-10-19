"""analysis/event_study.py — 이벤트 전후 N거래일 수익률·외국인 수급 측정 테스트 (손으로 계산 가능한 합성 데이터)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import event_study as es  # noqa: E402


def _close(n: int = 40, start: str = "2024-01-01") -> pd.Series:
    """close[i] = 100 + i (월~금 거래일)."""
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100.0 + np.arange(n), index=idx)


class TestReturns:
    def test_before_and_after_hand_computed(self):
        close = _close()
        t = close.index[20]  # close = 120
        r = es.event_study(close, [t], n_days=5)
        assert r.before.n == 1 and r.after.n == 1
        assert r.before.mean_pct == pytest.approx((120 / 115 - 1) * 100)
        assert r.after.mean_pct == pytest.approx((125 / 120 - 1) * 100)
        assert r.per_event[0]["ref_day"] == t.date().isoformat()

    def test_weekend_event_maps_to_previous_trading_day(self):
        close = _close()
        friday = close.index[19]
        assert friday.dayofweek == 4
        saturday = friday + pd.Timedelta(days=1)
        r = es.event_study(close, [saturday], n_days=5)
        assert r.per_event[0]["ref_day"] == friday.date().isoformat()
        assert r.after.mean_pct == pytest.approx((close.iloc[24] / close.iloc[19] - 1) * 100)

    def test_incomplete_windows_counted_per_window(self):
        close = _close(n=30)
        early = close.index[2]    # before 창(5일) 부족, after 가능
        late = close.index[27]    # after 창 부족, before 가능
        mid = close.index[15]
        r = es.event_study(close, [early, mid, late], n_days=5)
        assert r.before.n == 2 and r.after.n == 2     # 창마다 n이 다를 수 있고 따로 센다
        assert r.n_events == 3 and r.n_excluded == 0

    def test_future_and_pre_history_events_are_excluded(self):
        close = _close(n=30)
        future = close.index[-1] + pd.Timedelta(days=10)
        before_start = close.index[0] - pd.Timedelta(days=30)
        r = es.event_study(close, [future, before_start, close.index[15]], n_days=5)
        assert r.n_excluded == 2
        assert r.before.n == 1 and r.after.n == 1
        assert not any(e["event_date"] == future.date().isoformat() for e in r.per_event)

    def test_worst_is_most_negative_event_and_unreliable_when_small(self):
        idx = pd.bdate_range("2024-01-01", periods=60)
        prices = pd.Series(100.0, index=idx)
        prices.iloc[25:] = 90.0   # t=20 이벤트: after = -10%
        prices.iloc[45:] = 99.0   # t=40 이벤트: after = +10%
        r = es.event_study(prices, [idx[20], idx[40]], n_days=5)
        assert r.after.worst_pct == pytest.approx(-10.0)
        assert r.after.best_pct == pytest.approx(10.0)      # t=40: 90 → 99
        assert r.after.n_negative == 1
        assert r.after.n == 2 and r.after.reliable is False  # 원칙 3: n<30

    def test_reliable_when_enough_events(self):
        idx = pd.bdate_range("2020-01-01", periods=400)
        close = pd.Series(100 + np.random.RandomState(0).normal(0, 1, 400).cumsum(), index=idx)
        events = [idx[i] for i in range(10, 360, 10)]  # 35개
        r = es.event_study(close, events, n_days=5)
        assert r.after.n == 35 and r.after.reliable is True

    def test_no_events_gives_empty_stats_without_crash(self):
        r = es.event_study(_close(), [], n_days=5)
        assert r.before.n == 0 and np.isnan(r.before.mean_pct) and r.before.reliable is False
        assert r.flow_before.n == 0

    def test_empty_close(self):
        r = es.event_study(pd.Series(dtype=float), ["2024-01-05"], n_days=5)
        assert r.n_excluded == 1 and r.before.n == 0

    def test_invalid_window(self):
        with pytest.raises(ValueError):
            es.event_study(_close(), [], n_days=0)


class TestForeignFlow:
    def test_flow_sums_only_over_complete_windows(self):
        close = _close()
        flow = pd.Series(10.0, index=close.index)   # 매일 +10
        t = close.index[20]
        r = es.event_study(close, [t], n_days=5, foreigner=flow)
        assert r.flow_before.n == 1 and r.flow_before.mean == pytest.approx(50.0)
        assert r.flow_after.n == 1 and r.flow_after.mean == pytest.approx(50.0)

    def test_window_with_missing_day_is_skipped_not_partially_summed(self):
        close = _close()
        flow = pd.Series(10.0, index=close.index).drop(close.index[22])  # after 창(21~25)에 구멍
        t = close.index[20]
        r = es.event_study(close, [t], n_days=5, foreigner=flow)
        assert r.flow_after.n == 0                 # 4일치 합(=40)으로 지어내지 않는다
        assert r.flow_before.n == 1                # before 창(16~20)은 온전
        assert r.after.n == 1                      # 수익률 쪽 표본은 영향 없음

    def test_no_flow_data_reports_zero_samples(self):
        close = _close()
        for f in (None, pd.Series(dtype=float), pd.Series([np.nan] * len(close), index=close.index)):
            r = es.event_study(close, [close.index[20]], n_days=5, foreigner=f)
            assert r.flow_before.n == 0 and r.flow_after.n == 0
            assert r.before.n == 1

    def test_worst_flow_is_largest_net_sell(self):
        close = _close()
        flow = pd.Series(5.0, index=close.index)
        flow.iloc[21:26] = -100.0  # t=20의 after 창 → 합 -500
        flow.iloc[31:36] = 20.0    # t=30의 after 창 → 합 +100
        r = es.event_study(close, [close.index[20], close.index[30]], n_days=5, foreigner=flow)
        assert r.flow_after.worst == pytest.approx(-500.0)
        assert r.flow_after.n == 2
