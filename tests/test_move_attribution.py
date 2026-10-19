"""analysis/move_attribution.py — 급변일 기여분 분해 테스트 (합성 데이터)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import move_attribution as ma  # noqa: E402


def _series(rets_pct: np.ndarray, idx: pd.DatetimeIndex, base: float = 100.0) -> pd.Series:
    """일간 수익률(%) 배열에서 종가 시계열을 만든다(첫 값 = base)."""
    closes = base * np.cumprod(1 + np.concatenate([[0.0], rets_pct[1:]]) / 100)
    return pd.Series(closes, index=idx)


def _make_world(n: int = 160, seed: int = 0, shock_at: int = 140, shock_pct: float = -5.0):
    """
    000660 = 0.8*SOX(직전 미국 세션) + 0.5*USDKRW(당일) + 잡음. shock_at 날에 큰 개별 충격.
    SOX/NVDA/VIX/US10Y는 미국 영업일(한국 날짜와 하루 어긋남), KOSPI/USDKRW는 같은 날.
    """
    rs = np.random.RandomState(seed)
    kr_idx = pd.bdate_range("2025-01-01", periods=n)
    us_idx = kr_idx - pd.Timedelta(days=1)  # 한국 날짜보다 하루 앞선 미국 날짜(월요일은 일요일이 되지만 합성이라 무방)
    sox_r = rs.normal(0, 1.5, n)
    fx_r = rs.normal(0, 0.4, n)
    noise = rs.normal(0, 0.3, n)

    hynix_r = np.zeros(n)
    hynix_r[1:] = 0.8 * sox_r[1:] + 0.5 * fx_r[1:] + noise[1:]  # 한국 t일 = 그 전날 밤 마감한 미국 세션(us_idx[t])의 SOX
    hynix_r[shock_at] = shock_pct

    hynix = _series(hynix_r, kr_idx, 100000)
    indicators = {
        "SOX": _series(sox_r, us_idx, 5000),
        "USDKRW": _series(fx_r, kr_idx, 1380),
        "KOSPI": _series(0.6 * hynix_r + rs.normal(0, 0.5, n), kr_idx, 2600),
        "US10Y": pd.Series(4.0 + np.cumsum(rs.normal(0, 0.03, n)), index=us_idx),
    }
    return hynix, indicators, kr_idx


def _card_on(cards, hynix, pos=140):
    """충격일(위치 pos)의 카드. 무작위로 3%를 넘는 다른 날도 카드가 되므로 날짜로 고른다."""
    d = hynix.index[pos].date().isoformat()
    return next(c for c in cards if c.date == d)


class TestFindMoveDays:
    def test_threshold_inclusive_both_directions(self):
        idx = pd.bdate_range("2025-01-01", periods=5)
        prices = pd.Series([100, 103, 103, 99.91, 99.91], index=idx)  # +3.0%, 0, -3.0%(근사), 0
        days = ma.find_move_days(prices, threshold_pct=3.0)
        assert list(days.index) == [idx[1], idx[3]]
        assert days.iloc[0] > 0 > days.iloc[1]

    def test_no_moves_returns_empty(self):
        idx = pd.bdate_range("2025-01-01", periods=5)
        prices = pd.Series([100, 100.5, 101, 100.2, 100.9], index=idx)
        assert ma.find_move_days(prices).empty


class TestIdentity:
    def test_contributions_plus_residual_equals_actual(self):
        hynix, indicators, _ = _make_world()
        cards = ma.build_move_cards(hynix, indicators, lookback_days=200)
        assert cards, "충격일이 급변일로 잡혀야 한다"
        for card in cards:
            contrib_sum = sum(c.contribution_pct for c in card.contributions if c.contribution_pct is not None)
            assert contrib_sum == pytest.approx(card.explained_pct, abs=1e-9)
            assert card.explained_pct + card.residual_pct == pytest.approx(card.return_pct, abs=1e-9)

    def test_identity_holds_when_no_indicator_data(self):
        hynix, _, _ = _make_world()
        cards = ma.build_move_cards(hynix, {})
        assert cards
        for card in cards:
            assert card.explained_pct == 0.0
            assert card.residual_pct == pytest.approx(card.return_pct)
            assert all(c.status == ma.STATUS_NO_SERIES for c in card.contributions)
            assert all(c.contribution_pct is None for c in card.contributions)

    def test_shock_day_residual_is_large_and_known_driver_recovered(self):
        hynix, indicators, _ = _make_world(shock_pct=-6.0)
        no_kospi = tuple(s for s in ma.DEFAULT_INDICATORS if s.key != "KOSPI")
        cards = ma.build_move_cards(hynix, indicators, lookback_days=200, max_cards=50, specs=no_kospi)
        card = _card_on(cards, hynix)
        # 개별 충격은 (순환 지표인 코스피를 빼면) 설명 지표가 없으므로 대부분 잔차로 남는다.
        # (그날 SOX가 우연히 움직인 몫은 정당하게 기여분으로 빠지므로 -6%의 전부가 잔차는 아니다.)
        assert abs(card.residual_pct) > 3.0
        sox = next(c for c in card.contributions if c.key == "SOX")
        assert sox.beta == pytest.approx(0.8, abs=0.15)  # 합성으로 심은 베타를 대략 복원

    def test_kospi_circularity_absorbs_shock(self):
        # 코스피는 000660을 포함하므로 충격을 기여분으로 흡수한다 — 모듈 docstring의 한계를 고정해둔다.
        hynix, indicators, _ = _make_world(shock_pct=-6.0)
        card = _card_on(ma.build_move_cards(hynix, indicators, lookback_days=200, max_cards=50), hynix)
        kospi = next(c for c in card.contributions if c.key == "KOSPI")
        assert kospi.contribution_pct < -2.0
        assert kospi.note and "순환" in kospi.note


class TestAlignmentAndLookahead:
    def test_prior_us_session_is_strictly_before_kr_date(self):
        kr = pd.bdate_range("2025-03-03", periods=3)       # 월 화 수
        us_changes = pd.Series([1.0, 2.0, 3.0], index=kr)  # 같은 날짜에 미국 변화량이 찍혀 있다고 하자
        aligned = ma.align_to_kr_dates(us_changes, kr, ma.TIMING_PRIOR_US)
        # 월요일엔 직전 미국 거래일이 없다 → NaN, 화요일엔 월요일 값(1.0) — 같은 날 값은 절대 안 쓴다
        assert np.isnan(aligned["change"].iloc[0])
        assert aligned["change"].iloc[1] == 1.0
        assert aligned["change"].iloc[2] == 2.0
        assert aligned["source_date"].iloc[1] == kr[0]

    def test_stale_us_series_is_not_carried_forward(self):
        # 지표가 한 달 전에 끊겼는데 "직전 미국 세션"으로 그 오래된 값을 쓰면 안 된다.
        kr = pd.bdate_range("2025-03-03", periods=30)
        us_changes = pd.Series([1.0, 2.0], index=pd.to_datetime(["2025-03-03", "2025-03-04"]))
        aligned = ma.align_to_kr_dates(us_changes, kr, ma.TIMING_PRIOR_US)
        assert aligned["change"].iloc[1] == 1.0                       # 03-04 ← 03-03 (직전 세션)
        assert aligned["change"].iloc[2] == 2.0                       # 03-05 ← 03-04
        # 마지막 지표 날짜(03-04)에서 5일 이내인 03-06·03-07(2~3일 차이)은 허용, 03-10(6일)부터는 NaN
        assert aligned.loc["2025-03-06", "change"] == 2.0
        assert aligned.loc["2025-03-07", "change"] == 2.0
        assert aligned.loc["2025-03-10":, "change"].isna().all()

    def test_stale_series_in_card_is_no_change_not_old_value(self):
        hynix, indicators, _ = _make_world()
        stale = {"SOX": indicators["SOX"].iloc[:100]}  # 140번째 날 훨씬 전에 끊긴 SOX
        cards = ma.build_move_cards(hynix, stale, lookback_days=200, max_cards=50)
        sox = next(c for c in _card_on(cards, hynix).contributions if c.key == "SOX")
        assert sox.status == ma.STATUS_NO_CHANGE
        assert sox.contribution_pct is None and sox.change is None

    def test_same_day_uses_exact_date_only(self):
        kr = pd.bdate_range("2025-03-03", periods=3)
        ch = pd.Series([5.0], index=[kr[1]])
        aligned = ma.align_to_kr_dates(ch, kr, ma.TIMING_SAME_DAY)
        assert np.isnan(aligned["change"].iloc[0])
        assert aligned["change"].iloc[1] == 5.0
        assert np.isnan(aligned["change"].iloc[2])

    def test_beta_ignores_event_day_and_later_data(self):
        hynix, indicators, _ = _make_world(shock_pct=-5.0)
        day = hynix.index[140]
        base = _card_on(ma.build_move_cards(hynix, indicators, lookback_days=200, max_cards=50), hynix)
        assert base.date == day.date().isoformat()

        # 급변일 이후 데이터를 전부 이상한 값으로 바꿔도 급변일의 베타·기여분은 안 변해야 한다.
        tampered_h = hynix.copy()
        tampered_h.iloc[141:] = tampered_h.iloc[141:] * 3.0
        tampered_i = {k: v.copy() for k, v in indicators.items()}
        for v in tampered_i.values():
            v.iloc[142:] = v.iloc[142:] * 5.0
        again = _card_on(ma.build_move_cards(tampered_h, tampered_i, lookback_days=200, max_cards=50), hynix)
        for a, b in zip(base.contributions, again.contributions):
            assert a.beta == b.beta
            assert a.contribution_pct == b.contribution_pct
            assert a.beta_samples == b.beta_samples
        assert again.residual_pct == pytest.approx(base.residual_pct)

        # 급변일 당일 값을 바꾸면 급변일 수익률 자체가 달라지지만, 베타 추정 구간은 당일을 포함하지 않는다.
        for c in base.contributions:
            if c.beta_window_end:
                assert c.beta_window_end < base.date

    def test_beta_window_and_samples_always_reported(self):
        hynix, indicators, _ = _make_world()
        card = ma.build_move_cards(hynix, indicators, lookback_days=200, beta_window=60)[0]
        assert card.beta_window == 60
        for c in card.contributions:
            if c.status == ma.STATUS_OK:
                assert c.beta_samples == 60
                assert c.beta_window_start and c.beta_window_end


class TestDegradedData:
    def test_missing_indicator_is_marked_not_invented(self):
        hynix, indicators, _ = _make_world()
        card = ma.build_move_cards(hynix, indicators, lookback_days=200)[0]
        by_key = {c.key: c for c in card.contributions}
        assert by_key["NVDA"].status == ma.STATUS_NO_SERIES
        assert by_key["VIX"].status == ma.STATUS_NO_SERIES
        assert by_key["NVDA"].contribution_pct is None
        assert by_key["SOX"].status == ma.STATUS_OK

    def test_low_sample_beta_excluded_from_explained(self):
        hynix, indicators, kr = _make_world(n=160, shock_at=140)
        # 급변일 직전 20일에만 SOX가 존재 → 베타 표본 20 < 30
        short = {"SOX": indicators["SOX"].iloc[-25:]}
        card = ma.build_move_cards(hynix, short, lookback_days=200)[0]
        sox = next(c for c in card.contributions if c.key == "SOX")
        assert sox.status == ma.STATUS_LOW_SAMPLES
        assert sox.contribution_pct is None
        assert sox.beta_samples < ma.MIN_SAMPLES
        assert card.explained_pct == 0.0
        assert card.residual_pct == pytest.approx(card.return_pct)

    def test_constant_indicator_has_no_beta(self):
        hynix, indicators, kr = _make_world()
        flat = {"USDKRW": pd.Series(1380.0, index=kr)}
        card = ma.build_move_cards(hynix, flat, lookback_days=200)[0]
        fx = next(c for c in card.contributions if c.key == "USDKRW")
        assert fx.status in (ma.STATUS_NO_BETA, ma.STATUS_NO_CHANGE)
        assert fx.contribution_pct is None

    def test_too_short_history_gives_no_crash(self):
        idx = pd.bdate_range("2025-01-01", periods=3)
        prices = pd.Series([100.0, 90.0, 95.0], index=idx)
        cards = ma.build_move_cards(prices, {})
        assert len(cards) == 2  # -10%, +5.55%
        for card in cards:
            assert card.explained_pct + card.residual_pct == pytest.approx(card.return_pct)

    def test_empty_prices(self):
        assert ma.build_move_cards(pd.Series(dtype=float), {}) == []

    def test_no_move_days(self):
        idx = pd.bdate_range("2025-01-01", periods=40)
        prices = pd.Series(100 + np.arange(40) * 0.01, index=idx)
        assert ma.build_move_cards(prices, {}) == []


class TestFlowAndItems:
    def test_flow_only_when_row_exists(self):
        hynix, indicators, kr = _make_world()
        day = hynix.index[140]
        flow = pd.DataFrame({
            "date": [day], "individual": [100], "foreigner": [-500], "institution": [None],
        })
        card = _card_on(ma.build_move_cards(hynix, indicators, flow=flow, lookback_days=200, max_cards=50), hynix)
        assert card.flow == {"individual": 100.0, "foreigner": -500.0, "institution": None}

        card2 = _card_on(ma.build_move_cards(hynix, indicators, flow=None, lookback_days=200, max_cards=50), hynix)
        assert card2.flow is None
        empty = pd.DataFrame(columns=["date", "individual", "foreigner", "institution"])
        assert _card_on(ma.build_move_cards(hynix, indicators, flow=empty, lookback_days=200, max_cards=50), hynix).flow is None

    def test_flow_all_nan_row_is_none(self):
        hynix, indicators, _ = _make_world()
        day = hynix.index[140]
        flow = pd.DataFrame({"date": [day], "individual": [np.nan], "foreigner": [np.nan], "institution": [np.nan]})
        assert _card_on(ma.build_move_cards(hynix, indicators, flow=flow, lookback_days=200, max_cards=50), hynix).flow is None

    def test_items_attached_by_date_with_confidence(self):
        hynix, indicators, _ = _make_world()
        d = hynix.index[140].date().isoformat()
        items = {d: [{"kind": "claim", "text": "요약", "confidence": "rumor", "source": "누군가"}]}
        card = _card_on(ma.build_move_cards(hynix, indicators, items_by_date=items, lookback_days=200, max_cards=50), hynix)
        assert card.items[0]["confidence"] == "rumor"

    def test_max_cards_and_newest_first(self):
        idx = pd.bdate_range("2025-01-01", periods=60)
        r = np.zeros(60)
        for i in (10, 20, 30, 40, 50):
            r[i] = 4.0
        prices = _series(r, idx)
        cards = ma.build_move_cards(prices, {}, max_cards=3)
        assert [c.date for c in cards] == sorted([c.date for c in cards], reverse=True)
        assert len(cards) == 3
        assert cards[0].date == idx[50].date().isoformat()
