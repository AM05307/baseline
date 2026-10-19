"""
analysis/signals.py 테스트. 합성 데이터로만 검증한다(실제 수집 호출 없음).
"""

from __future__ import annotations

import pandas as pd
import pytest

from analysis import signals as sig


def _series(vals, start="2026-01-01") -> pd.Series:
    idx = pd.bdate_range(start, periods=len(vals))
    return pd.Series(vals, index=idx, dtype=float)


class TestKhj3DaySelloff:
    def test_turns_on_the_day_after_two_hard_down_days(self):
        # day0 baseline, day1 -4%, day2 -4%(연속 하드 다운) -> day3 on
        vals = [100, 96, 92.16, 92.16]  # day1: -4%, day2: -4%
        close = _series(vals)
        out = sig.khj_3day_selloff(close, day_pct=-3.0, two_day_pct=-8.0)
        assert out["is_on"].iloc[3] == 1
        assert out["is_on"].iloc[:3].sum() == 0

    def test_cumulative_trigger_without_each_day_hard(self):
        # day1 -1%, day2 -7.2%(합계 -8.1%, 둘 다 음수지만 day1은 -3% 기준 미충족)
        # -> day2가 셀오프 성립일, 그 다음날(day3)에 on
        vals = [100, 99, 91.9, 90]
        close = _series(vals)
        out = sig.khj_3day_selloff(close, day_pct=-3.0, two_day_pct=-8.0)
        two_day_ret = (close.iloc[2] / close.iloc[0] - 1) * 100
        assert two_day_ret <= -8.0
        assert out["is_on"].iloc[3] == 1
        assert out["is_on"].iloc[:3].sum() == 0

    def test_no_signal_when_not_two_consecutive_down_days(self):
        vals = [100, 96, 97]  # day1 하락, day2는 상승 -> 연속 하락 아님
        close = _series(vals)
        out = sig.khj_3day_selloff(close)
        assert out["is_on"].sum() == 0

    def test_changing_last_day_does_not_change_earlier_signal(self):
        vals = [100, 96, 92.16, 92.16, 95, 96, 97]
        close = _series(vals)
        close2 = close.copy()
        close2.iloc[-1] *= 1.5
        before = sig.khj_3day_selloff(close)
        after = sig.khj_3day_selloff(close2)
        assert before["is_on"].iloc[:-1].equals(after["is_on"].iloc[:-1])


class TestKhjConcentration:
    def test_counts_negative_markets_and_flags_threshold(self):
        idx = pd.bdate_range("2026-01-01", periods=70)
        up = pd.Series(range(100, 170), index=idx, dtype=float)
        down = pd.Series(range(170, 100, -1), index=idx, dtype=float)
        markets = {"A": up, "B": down, "C": down, "D": down, "E": down, "F": down}
        out = sig.khj_concentration(markets, lookback=63, n_threshold=5)
        assert out["value"].iloc[-1] == 5  # B,C,D,E,F 5개 마이너스
        assert out["is_on"].iloc[-1] == 1

    def test_below_threshold_is_off(self):
        idx = pd.bdate_range("2026-01-01", periods=70)
        up = pd.Series(range(100, 170), index=idx, dtype=float)
        down = pd.Series(range(170, 100, -1), index=idx, dtype=float)
        markets = {"A": up, "B": up, "C": down, "D": down}
        out = sig.khj_concentration(markets, lookback=63, n_threshold=5)
        assert out["is_on"].iloc[-1] == 0


class TestKhjOverheat:
    def test_volume_spike_triggers_on(self):
        idx = pd.bdate_range("2026-01-01", periods=25)
        close = pd.Series(100.0, index=idx)
        volume = pd.Series(1000.0, index=idx)
        volume.iloc[-1] = 5000.0  # 20일 평균의 2배 이상
        out = sig.khj_overheat(close, volume)
        assert out["is_on"].iloc[-1] == 1

    def test_price_premium_triggers_on(self):
        idx = pd.bdate_range("2026-01-01", periods=25)
        close = pd.Series(100.0, index=idx)
        close.iloc[-1] = 120.0  # 20일선보다 훨씬 높음
        volume = pd.Series(1000.0, index=idx)
        out = sig.khj_overheat(close, volume)
        assert out["is_on"].iloc[-1] == 1

    def test_flat_market_is_off(self):
        idx = pd.bdate_range("2026-01-01", periods=25)
        close = pd.Series(100.0, index=idx)
        volume = pd.Series(1000.0, index=idx)
        out = sig.khj_overheat(close, volume)
        assert out["is_on"].iloc[-1] == 0


class TestKhjRateStage:
    def test_only_previous_peak_stage_is_on(self):
        idx = pd.bdate_range("2026-01-01", periods=4)
        stages = pd.Series(["인상 전", "1회", "2~3회", "이전 고점 도달"], index=idx)
        out = sig.khj_rate_stage(stages)
        assert list(out["is_on"]) == [0, 0, 0, 1]


class TestKhjIpoWeak:
    def test_on_when_ipo_lags_benchmark(self):
        idx = pd.bdate_range("2026-01-01", periods=70)
        bench = pd.Series(range(100, 170), index=idx, dtype=float)  # +69%ish
        ipo = pd.Series(100.0, index=idx)  # 0% 변화
        out = sig.khj_ipo_weak(ipo, bench, lookback=63, gap_threshold_pct=-10.0)
        assert out["is_on"].iloc[-1] == 1

    def test_off_when_ipo_keeps_pace(self):
        idx = pd.bdate_range("2026-01-01", periods=70)
        bench = pd.Series(range(100, 170), index=idx, dtype=float)
        ipo = bench.copy()
        out = sig.khj_ipo_weak(ipo, bench, lookback=63, gap_threshold_pct=-10.0)
        assert out["is_on"].iloc[-1] == 0


class TestKhjCombo:
    def test_counts_simultaneous_signals(self):
        idx = pd.bdate_range("2026-01-01", periods=3)
        rate_on = pd.Series([1, 0, 1], index=idx)
        conc_on = pd.Series([1, 0, 1], index=idx)
        ipo_on = pd.Series([0, 0, 1], index=idx)
        out = sig.khj_combo(rate_on, conc_on, ipo_on)
        assert list(out["value"]) == [2, 0, 3]
        assert list(out["is_on"]) == [1, 0, 1]


class TestYjhBoxRange:
    def test_buy_candidate_near_range_bottom(self):
        idx = pd.bdate_range("2026-01-01", periods=61)
        vals = list(range(100, 160)) + [101]  # 범위 100~159, 마지막날 101(하단 근처)
        close = pd.Series(vals, index=idx, dtype=float)
        out = sig.yjh_box_range(close, lookback=60, band_pct=20.0)
        assert out["is_on_buy"].iloc[-1] == 1
        assert out["is_on_trim"].iloc[-1] == 0

    def test_trim_candidate_near_range_top(self):
        idx = pd.bdate_range("2026-01-01", periods=61)
        vals = list(range(100, 160)) + [158]
        close = pd.Series(vals, index=idx, dtype=float)
        out = sig.yjh_box_range(close, lookback=60, band_pct=20.0)
        assert out["is_on_trim"].iloc[-1] == 1
        assert out["is_on_buy"].iloc[-1] == 0


class TestYjhUs10y:
    def test_on_above_threshold(self):
        tnx = _series([4.0, 4.6, 5.0])
        out = sig.yjh_us10y(tnx, threshold_pct=4.5)
        assert list(out["is_on"]) == [0, 1, 1]


class TestYjhMemoryMargin:
    def test_on_only_after_peaking_then_falling(self):
        idx = pd.bdate_range("2026-01-01", periods=5)
        margin = pd.Series([70, 82, 75, 58, 59], index=idx, dtype=float)
        out = sig.yjh_memory_margin(margin, peak_pct=80.0, trough_pct=60.0)
        # idx0:70(피크 전,조건불충분) idx1:82(피크지만 60이하 아님) idx2:75 idx3:58(피크했었고 60이하->on) idx4:59(on)
        assert list(out["is_on"]) == [0, 0, 0, 1, 1]

    def test_off_when_never_peaked(self):
        idx = pd.bdate_range("2026-01-01", periods=3)
        margin = pd.Series([65, 60, 55], index=idx, dtype=float)
        out = sig.yjh_memory_margin(margin, peak_pct=80.0, trough_pct=60.0)
        assert out["is_on"].sum() == 0


class TestYjhPbrFloor:
    def test_on_near_floor(self):
        pbr = _series([1.5, 1.2, 1.05, 0.95])
        out = sig.yjh_pbr_floor(pbr, floor_threshold=1.1)
        assert list(out["is_on"]) == [0, 0, 1, 1]


class TestKsjPanicDay:
    def test_on_the_day_after_panic(self):
        vals = [100, 94, 94]  # day1 -6%(패닉), day2는 패닉 다음날 -> on
        close = _series(vals)
        out = sig.ksj_panic_day(close, threshold_pct=-5.0)
        assert list(out["is_on"]) == [0, 0, 1]


class TestKsjBreadth:
    def test_on_when_only_semis_carry_the_index(self):
        idx = pd.bdate_range("2026-01-01", periods=25)
        kospi = pd.Series(range(100, 125), index=idx, dtype=float)
        sec = pd.Series(range(100, 300, 8), index=idx, dtype=float)[:25]
        hynix = pd.Series(range(100, 300, 8), index=idx, dtype=float)[:25]
        out = sig.ksj_breadth(kospi, sec, hynix, lookback=20)
        assert out["is_on"].iloc[-1] == 1

    def test_off_when_broad_market_also_rises(self):
        idx = pd.bdate_range("2026-01-01", periods=25)
        kospi = pd.Series(range(100, 125), index=idx, dtype=float)
        out = sig.ksj_breadth(kospi, kospi, kospi, lookback=20)
        assert out["is_on"].iloc[-1] == 0

    def test_handles_series_with_different_date_ranges(self):
        # 회귀 테스트: 세 시리즈의 수집 시작일이 서로 다르면(실제 수집 데이터처럼,
        # KOSPI가 더 오래전부터 쌓여 있는 경우) 정렬 안 된 인덱스 비교 에러 없이
        # 겹치는 구간만으로 계산돼야 한다.
        long_idx = pd.bdate_range("2026-01-01", periods=40)  # KOSPI: 더 길게 쌓여 있음
        short_idx = long_idx[15:]  # 005930/000660은 그 중 뒷부분만(수집 시작이 늦었다고 가정), long_idx와 완전히 겹침
        kospi = pd.Series(range(100, 140), index=long_idx, dtype=float)
        sec = pd.Series(range(100, 300, 8), index=short_idx, dtype=float)
        hynix = pd.Series(range(100, 300, 8), index=short_idx, dtype=float)
        out = sig.ksj_breadth(kospi, sec, hynix, lookback=20)  # 에러 없이 끝나야 함
        assert not out.empty
        assert out.index.min() >= short_idx.min()  # 공통 구간(더 짧은 쪽)으로 맞춰짐
        assert out.index.max() <= short_idx.max()


class TestKsjUs10yHigh:
    def test_on_above_threshold(self):
        tnx = _series([4.5, 4.9])
        out = sig.ksj_us10y_high(tnx, threshold_pct=4.8)
        assert list(out["is_on"]) == [0, 1]


class TestKsjForeignSell:
    def test_on_after_window_of_net_selling(self):
        # 20일 내내 순매도(-10)면 누적 -200 <= 0 -> 그 다음날 on
        vals = [-10.0] * 21
        s = _series(vals)
        out = sig.ksj_foreign_sell(s, window=20, sell_threshold=0.0)
        assert out["is_on"].iloc[20] == 1
        assert out["value"].iloc[20] == pytest.approx(-200.0)

    def test_off_when_net_buying(self):
        vals = [10.0] * 21  # 계속 순매수 -> 누적 양수 -> off
        s = _series(vals)
        out = sig.ksj_foreign_sell(s, window=20, sell_threshold=0.0)
        assert out["is_on"].iloc[20] == 0

    def test_no_lookahead_last_day_change_does_not_affect_earlier_signal(self):
        vals = [-10.0] * 21
        s = _series(vals)
        out_before = sig.ksj_foreign_sell(s, window=20, sell_threshold=0.0)
        s2 = s.copy()
        s2.iloc[-1] = 500.0  # 마지막 날 값을 바꿔도
        out_after = sig.ksj_foreign_sell(s2, window=20, sell_threshold=0.0)
        assert out_before["is_on"].iloc[20] == out_after["is_on"].iloc[20]  # 이전 신호는 불변

    def test_empty_input_returns_empty_not_fabricated(self):
        empty = pd.Series(dtype=float)
        out = sig.ksj_foreign_sell(empty)
        assert out.empty
