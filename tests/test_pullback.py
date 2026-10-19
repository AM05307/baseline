"""
눌림목 지정가 매매(analysis/pullback.py) 테스트.

전부 합성 OHLC로 검증한다(실제 yfinance 호출 없음). 체결가/체결 여부를
손으로 계산할 수 있을 만큼 작은 숫자로 구성했다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from analysis import pullback as pb


def _ohlc(rows: list[tuple[float, float, float, float]], start: str = "2026-01-01") -> pd.DataFrame:
    """rows: [(open, high, low, close), ...] -> DataFrame(index=영업일)."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


class TestLimitOrderFill:
    def test_fills_at_limit_price_when_low_touches_it(self):
        # day0: 기준 종가 100. day1: 전일종가*0.98=98이 지정가. 시가100은 지정가보다
        # 높지만 저가97이 지정가 밑으로 닿으므로 지정가(98) 그대로 체결.
        rows = [
            (100, 100, 100, 100),
            (100, 101, 97, 99),
            (99, 99.5, 98.7, 99.3),  # max_hold_days=1로 강제 청산시켜서 체결가를 확인
        ]
        trades = pb.simulate_pullback_trades(_ohlc(rows), max_hold_days=1)
        assert len(trades) == 1
        assert trades[0].entry_price == pytest.approx(98.0)
        assert trades[0].entry_date == _ohlc(rows).index[1]

    def test_fills_at_open_when_open_already_below_limit(self):
        # day1 시가96이 이미 지정가98보다 낮게 열렸으므로, 지정가가 아니라
        # 더 유리한 시가(96)에 체결된다.
        rows = [
            (100, 100, 100, 100),
            (96, 97, 95, 96.5),
            (97, 97.5, 96.7, 97.3),
        ]
        trades = pb.simulate_pullback_trades(_ohlc(rows), max_hold_days=1)
        assert len(trades) == 1
        assert trades[0].entry_price == pytest.approx(96.0)

    def test_no_fill_when_low_never_reaches_limit(self):
        rows = [
            (100, 100, 100, 100),
            (100, 102, 99, 101),  # 저가99 > 지정가98 -> 체결 안 됨
        ]
        trades = pb.simulate_pullback_trades(_ohlc(rows), max_hold_days=5)
        assert trades == []


class TestGapSkip:
    def test_gap_up_skips_order_even_if_low_would_have_filled(self):
        # day1 시가가 전일종가 대비 +3%(gap_skip_pct=0.02 초과) 갭상승 ->
        # 그날은 주문 자체를 안 낸다. 저가가 아무리 떨어져도 체결되지 않는다.
        rows = [
            (100, 100, 100, 100),
            (103, 104, 90, 102),  # 갭+3%, 저가는 지정가(98) 밑으로 떨어짐 -> 그래도 스킵
            (102, 103, 101, 102),  # 갭 없음, 지정가=102*0.98=99.96, 저가101 > 지정가 -> 역시 체결 안 됨
        ]
        trades = pb.simulate_pullback_trades(_ohlc(rows), max_hold_days=5)
        assert trades == []

    def test_small_gap_under_threshold_still_places_order(self):
        rows = [
            (100, 100, 100, 100),
            (101, 101, 97, 99),  # 갭 +1% (<2% 임계값) -> 주문 냄. 지정가98, 저가97 체결.
            (99, 99.5, 98.7, 99.3),
        ]
        trades = pb.simulate_pullback_trades(_ohlc(rows), max_hold_days=1)
        assert len(trades) == 1
        assert trades[0].entry_price == pytest.approx(98.0)


class TestSameDayTakeProfitAndStopLoss:
    def test_stop_loss_wins_when_both_touched_same_day(self):
        # 진입가98(day1 체결). day2: 익절가=98*1.02=99.96, 손절가=98*0.97=95.06.
        # day2 고가101(익절선 넘음)·저가94(손절선 아래) 둘 다 닿음 -> 손절 우선.
        rows = [
            (100, 100, 100, 100),
            (100, 101, 97, 99),
            (99, 101, 94, 96),
        ]
        trades = pb.simulate_pullback_trades(_ohlc(rows))
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "stop_loss"
        assert t.exit_price == pytest.approx(98 * 0.97)

    def test_take_profit_only_when_stop_not_touched(self):
        rows = [
            (100, 100, 100, 100),
            (100, 101, 97, 99),
            (99, 101, 97.5, 100),  # 저가97.5는 손절가(95.06) 위 -> 손절 안 닿음. 고가101>=익절가99.96
        ]
        trades = pb.simulate_pullback_trades(_ohlc(rows))
        assert len(trades) == 1
        assert trades[0].exit_reason == "take_profit"
        assert trades[0].exit_price == pytest.approx(98 * 1.02)

    def test_no_stop_loss_configured_never_triggers_it(self):
        # stop_loss_pct=None이면 저가가 아무리 떨어져도 손절로 청산되지 않는다
        # (익절이나 max_hold로만 청산).
        rows = [
            (100, 100, 100, 100),
            (100, 101, 97, 99),
            (99, 99.5, 50, 99.3),  # 저가가 폭락해도 손절 없음 -> 그날은 아무 조건도 안 닿아 보유 지속
            (99, 99.3, 98.9, 99.1),
        ]
        trades = pb.simulate_pullback_trades(_ohlc(rows), stop_loss_pct=None, max_hold_days=2)
        assert len(trades) == 1
        assert trades[0].exit_reason == "max_hold"


class TestMaxHoldDays:
    def test_forced_exit_at_close_after_max_hold_days(self):
        rows = [
            (100, 100, 100, 100),
            (100, 101, 97, 99),      # day1: 진입(98 체결)
            (99, 99.5, 98.7, 99.3),  # day2: hold_days=1, tp/sl 안 닿음(max_hold_days=2라 아직 유지)
            (99.3, 99.6, 99.0, 99.4),  # day3: hold_days=2 == max_hold_days -> 종가 강제 청산
        ]
        df = _ohlc(rows)
        trades = pb.simulate_pullback_trades(df, take_profit_pct=0.5, stop_loss_pct=0.5, max_hold_days=2)
        assert len(trades) == 1
        t = trades[0]
        assert t.exit_reason == "max_hold"
        assert t.exit_date == df.index[3]
        assert t.exit_price == pytest.approx(99.4)
        assert t.hold_days == 2


class TestRegimeClassification:
    def test_known_trailing_returns_classify_correctly(self):
        vals = [100, 100, 100, 112, 100, 90, 100, 100, 100, 100]
        idx = pd.bdate_range("2026-01-01", periods=len(vals))
        close = pd.Series(vals, index=idx, dtype=float)

        regime = pb.classify_regime(close, lookback=3, up_threshold_pct=8.0, down_threshold_pct=-8.0)

        # 처음 lookback(3)일은 분류 불가
        assert pd.isna(regime.iloc[0])
        assert pd.isna(regime.iloc[1])
        assert pd.isna(regime.iloc[2])
        # idx3: 112/100-1=+12% -> uptrend
        assert regime.iloc[3] == "uptrend"
        # idx4: 100/100-1=0% -> box
        assert regime.iloc[4] == "box"
        # idx5: 90/100-1=-10% -> downtrend
        assert regime.iloc[5] == "downtrend"
        # idx6: 100/112-1≈-10.7% -> downtrend
        assert regime.iloc[6] == "downtrend"
        # idx8: 100/90-1≈+11.1% -> uptrend
        assert regime.iloc[8] == "uptrend"

    def test_changing_last_day_does_not_change_earlier_classification(self):
        vals = [100, 100, 100, 112, 100, 90, 100, 100, 100, 100]
        idx = pd.bdate_range("2026-01-01", periods=len(vals))
        close = pd.Series(vals, index=idx, dtype=float)
        close2 = close.copy()
        close2.iloc[-1] *= 3

        before = pb.classify_regime(close, lookback=3)
        after = pb.classify_regime(close2, lookback=3)
        assert list(before.iloc[:-1]) == list(after.iloc[:-1])


class TestRegimeBreakdownBuyAndHold:
    def test_classification_day_own_return_is_not_double_counted(self):
        # day4가 '상승추세'로 분류되는 이유는 day3->day4의 +30% 급등이다.
        # 그 +30%를 그대로 '상승추세 buy&hold 수익'에 되돌려 집계하면 분류를
        # 만든 수익을 다시 세는 순환 참조가 된다 — 반드시 day4 자신의
        # 수익률(+30%)이 아니라 day4->day5(다음날) 수익률이 집계돼야 한다.
        vals = [100, 100, 100, 100, 130, 130, 130, 130]
        idx = pd.bdate_range("2026-01-01", periods=len(vals))
        close = pd.Series(vals, index=idx, dtype=float)

        regime = pb.classify_regime(close, lookback=3, up_threshold_pct=8.0, down_threshold_pct=-8.0)
        assert list(regime.iloc[3:7]) == ["box", "uptrend", "uptrend", "uptrend"]

        breakdown = pb.regime_breakdown([], close, lookback=3, up_threshold_pct=8.0, down_threshold_pct=-8.0)
        # day4,5,6이 uptrend고 각각의 다음날(day5,6,7) 수익률은 전부 0% ->
        # 체이닝해도 0%. +30%(day3->day4, day4 자신을 만든 수익)는 전혀
        # 섞여 들어가면 안 된다.
        assert breakdown["uptrend"].buy_and_hold_pct == pytest.approx(0.0)

    def test_last_day_excluded_since_no_next_day_return_exists(self):
        # 마지막 날은 다음날 수익률이 없으므로(데이터 끝) buy&hold 집계에서
        # 자연히 빠져야 한다 — 에러 없이 조용히 제외.
        vals = [100, 100, 100, 100, 130]
        idx = pd.bdate_range("2026-01-01", periods=len(vals))
        close = pd.Series(vals, index=idx, dtype=float)
        regime = pb.classify_regime(close, lookback=3, up_threshold_pct=8.0, down_threshold_pct=-8.0)
        assert regime.iloc[-1] == "uptrend"  # 마지막 날 자체는 분류는 됨

        breakdown = pb.regime_breakdown([], close, lookback=3, up_threshold_pct=8.0, down_threshold_pct=-8.0)
        # uptrend로 분류된 유일한 날(마지막 날)이 다음날 수익률이 없어 전부
        # 제외되므로 버킷이 비어 nan이어야 한다(에러가 나면 안 됨).
        assert breakdown["uptrend"].buy_and_hold_pct != breakdown["uptrend"].buy_and_hold_pct  # nan


class TestRegimeBreakdownTradeStats:
    def test_mean_median_worst_are_expectancy_independent_of_buy_and_hold(self):
        idx = pd.bdate_range("2026-01-01", periods=30)
        close = pd.Series([100.0] * 30, index=idx, dtype=float)  # 완전히 평평 -> 전부 box, buy&hold=0%

        def trade(entry_idx: int, return_pct: float) -> pb.PullbackTrade:
            return pb.PullbackTrade(
                entry_date=idx[entry_idx], exit_date=idx[entry_idx + 1],
                entry_price=100.0, exit_price=100.0, exit_reason="take_profit",
                hold_days=1, return_pct=return_pct,
            )

        # box 구간(분류 가능한 lookback 이후 날짜)에 거래 3개: +10, +2, -30
        trades = [trade(25, 10.0), trade(26, 2.0), trade(27, -30.0)]
        breakdown = pb.regime_breakdown(trades, close, lookback=3, up_threshold_pct=8.0, down_threshold_pct=-8.0)
        box = breakdown["box"]

        assert box.n == 3
        assert box.mean_return_pct == pytest.approx((10.0 + 2.0 - 30.0) / 3)
        assert box.median_return_pct == pytest.approx(2.0)
        assert box.worst_pct == pytest.approx(-30.0)
        # buy&hold는 가격이 평평해서 0% -- 기대값(mean/median/worst)과 전혀 무관하게 계산됨을 확인
        assert box.buy_and_hold_pct == pytest.approx(0.0)
