"""국장/미장 비교 백테스트(analysis/market_compare) — 합성 가격으로 손계산 가능한 경우만."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import market_compare as mc  # noqa: E402

NO_COST = mc.MarketCosts(0.0, lambda d: 0.0)


def _px(values, start="2020-01-01"):
    idx = pd.bdate_range(start, periods=len(values))
    s = pd.Series(np.asarray(values, float), index=idx)
    return s, s.copy()  # 시가 = 종가(갭 0)


def test_to_krw_uses_as_of_rate():
    usd = pd.Series([10.0, 10.0, 10.0], index=pd.DatetimeIndex(["2020-01-02", "2020-01-03", "2020-01-06"]))
    fx = pd.Series([1000.0, 1100.0], index=pd.DatetimeIndex(["2020-01-01", "2020-01-03"]))  # 1/6은 직전(1/3) 환율
    assert list(mc.to_krw(usd, fx)) == [10000.0, 11000.0, 11000.0]
    assert list(mc.to_krw(usd, None)) == [10.0, 10.0, 10.0]


def test_buy_and_hold_tracks_price_and_after_tax_uses_deduction():
    o, c = _px([100, 110, 120, 200])
    us = mc.MarketCosts(0.0, lambda d: 0.0, cg_tax_rate=0.22, cg_deduction=100.0)
    r = mc.simulate("A", o, c, us, initial=1000.0, monthly=0.0)
    assert r.final_value == pytest.approx(2000.0)
    assert r.after_tax_value == pytest.approx(2000.0 - (1000.0 - 100.0) * 0.22)  # 이익 1000 − 공제 100
    m = mc.metrics("A", r)
    assert m.cum == pytest.approx(1.0) and m.after_tax_cum == pytest.approx(1.0 - 0.198)


def test_domestic_costs_reduce_value():
    o, c = _px([100, 100])
    kr = mc.MarketCosts(0.001, lambda d: 0.002)
    r = mc.simulate("A", o, c, kr, initial=1000.0, monthly=0.0)
    assert r.final_value == pytest.approx(1000 / 1.001)
    assert r.after_tax_value == pytest.approx(1000 / 1.001 * (1 - 0.003))


def test_monthly_contribution_on_first_trading_day_and_xirr():
    idx = pd.bdate_range("2020-01-01", "2021-12-31")
    price = pd.Series(100 * 1.10 ** ((idx - idx[0]).days / 365.25), index=idx)  # 연 10% 꾸준히
    r = mc.simulate("B", price, price, NO_COST, initial=0.0, monthly=100.0)
    assert len(r.contributions) == 24 and all(d.day <= 3 for d, _ in r.contributions)
    m = mc.metrics("B", r)
    assert m.cagr == pytest.approx(0.10, abs=0.003) and m.contributed == 2400.0
    assert m.mdd == pytest.approx(0.0, abs=1e-9)  # 시간가중 지수는 적립으로 흔들리지 않는다


def test_drawdown_tranches_buy_at_next_open_once():
    o, c = _px([100, 95, 89, 85, 79, 70, 100, 60])
    o.iloc[3] = 88  # 2일째 종가 89(−11%) → 3일째 시가 88에 1몫
    r = mc.simulate("D", o, c, NO_COST, initial=400.0, monthly=0.0, dd_initial=0.25, dd_levels=(-0.10, -0.20, -0.30))
    assert r.trades == 4 and not r.notes  # 첫날 + 세 몫(−10/−20/−30%), 다시 떨어져도 더 안 삼
    first_tranche_qty = 100.0 / 88
    assert r.equity.index[3] in r.equity.index and first_tranche_qty > 1.13


def test_drawdown_unused_tranches_reported_as_cash():
    o, c = _px([100, 101, 102, 103])
    r = mc.simulate("D", o, c, NO_COST, initial=400.0, monthly=0.0)
    assert r.notes and r.invested_share_end < 0.3


def test_c_follows_previous_close_decision_and_rebalances_only_on_change():
    o, c = _px([100, 100, 100, 200, 200])
    dec = pd.Series([1.0, 0.0, 0.0, 0.0, 0.0], index=c.index)  # 1번 날 종가 결정 0 → 2번 날 시가(100) 매도
    r = mc.simulate("C", o, c, NO_COST, initial=1000.0, monthly=0.0, decided_weight=dec)
    assert r.trades == 2 and r.final_value == pytest.approx(1000.0)  # 200 상승 전에 팔았다
    with pytest.raises(ValueError):
        mc.simulate("C", o, c, NO_COST, initial=1.0, monthly=0.0)


def test_longest_recovery_and_unrecovered_flag():
    idx = pd.DatetimeIndex(["2020-01-01", "2020-01-11", "2020-01-21", "2020-02-01", "2020-03-01"])
    assert mc.longest_recovery_days(pd.Series([1, 0.8, 1.05, 0.9, 0.95], index=idx)) == (40, True)
    assert mc.longest_recovery_days(pd.Series([1, 0.8, 1.05, 1.1, 1.2], index=idx)) == (20, False)


def test_script_runs_on_synthetic_db(tmp_path, monkeypatch, capsys):
    from db import store
    from scripts import market_compare
    db = tmp_path / "b.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init_db(db)
    idx = pd.bdate_range("2004-06-01", "2021-06-30")
    rows = []
    for t, base in (("000660", 100), ("069500", 50), ("SPY", 20), ("QQQ", 10), ("KOSPI", 1000), ("USDKRW", 1100)):
        rows += [{"ticker": t, "date": d.strftime("%Y-%m-%d"), "open": base * (1 + 0.0002 * i), "high": 1, "low": 1,
                  "close": base * (1 + 0.0002 * i), "volume": 1, "source": "t"} for i, d in enumerate(idx)]
    store.upsert_prices(rows)
    store.upsert_signal({"signal_id": "YJH_US10Y", "expert_id": "E", "name": "x", "kind": "t", "direction": "risk",
                         "description": "", "enabled": 1})
    store.upsert_signal_values([{"date": d.strftime("%Y-%m-%d"), "signal_id": "YJH_US10Y", "value": 1.0,
                                 "is_on": int(i % 9 == 0)} for i, d in enumerate(idx)])
    cfg = market_compare.load_config()
    cfg["start"] = "2015-01-02"
    text = market_compare.report(cfg)
    for needle in ("A 단순 보유", "B 매달 적립", "C 위험점수 비중 조절", "D 고점 대비 하락 분할매수", "시작 연도별", "최신 세법"):
        assert needle in text
    for banned in ("오를 것", "내릴 것", "매수하세요", "매도하세요", "nan", "None"):
        assert banned not in text
