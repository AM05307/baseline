"""
국장/미장 비교 백테스트 — 000660 · 069500 · SPY · QQQ × 전략 A~D, 원화 기준. DB 읽기만(네트워크 없음).

    python scripts/market_compare.py                 # config/market_compare.yaml
    python scripts/market_compare.py --out reports/market_compare.md

가정·정의는 analysis/market_compare.py docstring. "어느 시장이 낫다"는 결론을 내지 않는다 — 표만.
단순 보유(A)보다 연환산 수익률이 낮은 전략에는 ▼ 표시(B는 돈의 가중 수익률이라 A의 시간가중 수익률과 정의가 다르다는 점도 표에 적음).
C의 기준점은 기준 기간(2005~2018)에서 정했으므로, 2018년 이전이 포함된 결과는 표본 안 구간을 포함한다(표에 표시).
과거 데이터에 대한 측정이며 향후 수익을 보장하지 않는다. 매수·매도 추천이 아니다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import group_score as gs  # noqa: E402
from analysis import market_compare as mc  # noqa: E402
from analysis import signal_alignment as sa  # noqa: E402
from analysis import trading_costs as tc  # noqa: E402
from analysis import what_if as wi  # noqa: E402
from db import store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "market_compare.yaml"


def load_config(path: Path = CONFIG) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def market_costs(ticker: str, cfg: dict) -> mc.MarketCosts:
    c = cfg["costs"]
    if ticker in cfg["usd_targets"]:
        t = cfg["overseas_capital_gains_tax"]
        return mc.MarketCosts(float(c["us_commission"]), lambda d: 0.0, float(c["fx_spread"]), float(t["rate"]),
                              float(t["annual_deduction_krw"]))
    if ticker == "000660":
        return mc.MarketCosts(float(c["kr_commission"]), tc.tax_rate_on)
    etf_tax = float(c["kr_etf_sell_tax"])
    return mc.MarketCosts(float(c["kr_commission"]), lambda d: etf_tax)


def load_prices(ticker: str, cfg: dict, fx: pd.Series | None) -> tuple[pd.Series, pd.Series]:
    ohlc = store.get_ohlc(ticker).sort_index()
    ohlc = ohlc[ohlc["close"].notna()]
    open_ = ohlc["open"].where(ohlc["open"] > 0).fillna(ohlc["close"].shift(1)).fillna(ohlc["close"])  # 시가 없으면 전일 종가
    use_fx = fx if ticker in cfg["usd_targets"] else None
    return mc.to_krw(open_, use_fx), mc.to_krw(ohlc["close"], use_fx)


def decided_weights(ticker: str, cfg: dict, dates: pd.DatetimeIndex) -> tuple[pd.Series, gs.Thresholds]:
    """C 전략: 대상별 제외 신호를 뺀 그룹 점수 → 기준 기간 백분위 단계 → 비중. 국내는 한국 거래일로 기준점, 해외는 그 대상 거래일로."""
    sig = store.get_signals(enabled_only=True)
    ids = list(sig.loc[sig["direction"].isin(["risk", "opportunity"]), "signal_id"])
    values = {s: store.get_signal_values(s) for s in ids}
    us = ticker in cfg["usd_targets"]
    grid = dates if us else dates.union(store.get_prices("KOSPI").index)
    # 해외 대상은 미국 장 마감 뒤 결정 — 해외 세션 신호도 같은 날 값을 쓸 수 있다(국내는 결정일 이전 값만)
    is_on, _ = sa.align_signals(values, grid, frozenset() if us else sa.PRIOR_SESSION_SIGNALS)
    st = wi.group_states(is_on, grid, exclude=frozenset(cfg["exclude_signals"].get(ticker, [])))
    w = st["stage"].map(dict(enumerate(cfg["stage_weights"]))).astype(float)
    return w, st.attrs["thresholds"]


def run_all(cfg: dict, start: str | None = None) -> dict:
    fx = store.get_prices("USDKRW").sort_index()
    out = {"targets": {}, "start": start or cfg["start"]}
    for t in cfg["targets"]:
        o, c = load_prices(t, cfg, fx)
        if c.empty:
            out["targets"][t] = None
            continue
        weights, thr = decided_weights(t, cfg, c.index)
        out["targets"][t] = {"open": o, "close": c, "weights": weights, "thresholds": thr, "costs": market_costs(t, cfg)}
    return out


def run_one(data: dict, strategy: str, start: str, cfg: dict, end: str | None = None) -> tuple[mc.SimResult, mc.Metrics]:
    o, c = data["open"], data["close"]
    m = c.index >= pd.Timestamp(start)
    if end:
        m &= c.index <= pd.Timestamp(end)
    r = mc.simulate(strategy, o[m], c[m], data["costs"], initial=float(cfg["initial_capital_krw"]),
                    monthly=float(cfg["monthly_contribution_krw"]), decided_weight=data["weights"],
                    dd_initial=float(cfg["drawdown_buy"]["initial_fraction"]),
                    dd_levels=tuple(float(x) for x in cfg["drawdown_buy"]["levels"]))
    return r, mc.metrics(strategy, r)


def _p(x: float) -> str:
    return "데이터 없음" if x != x else f"{x * 100:+.2f}%"


def report(cfg: dict, common_end: str | None = None) -> str:
    data = run_all(cfg)
    ends = [d["close"].index[-1] for d in data["targets"].values() if d is not None]
    end = common_end or min(ends).date().isoformat()
    start = cfg["start"]
    L: list[str] = []
    L.append(f"# 국장/미장 비교 백테스트 (원화 기준)\n\n기간 {start} ~ {end} (네 대상 공통 마지막 거래일) · "
             f"첫날 {cfg['initial_capital_krw']:,}원(A·C·D) · 매달 {cfg['monthly_contribution_krw']:,}원(B) — 계산용 가정값")
    L.append("가격 수익률(배당 미포함) · 소수점 수량 · t일 종가 결정 → t+1일 시가 체결(C·D) · 현금 이자 0 · "
             "해외는 실제 USD/KRW 일별 환율(as-of)로 원화 환산")
    c = cfg["costs"]
    t = cfg["overseas_capital_gains_tax"]
    L.append(f"비용: 국내 수수료 {c['kr_commission']:.3%} · 000660 매도세 스케줄 · 069500 매도세 {c['kr_etf_sell_tax']:.2%} · "
             f"해외 수수료 {c['us_commission']:.2%} · 환전 {c['fx_spread']:.2%} · 해외 양도세 {t['rate']:.0%}"
             f"(연 {t['annual_deduction_krw']:,}원 공제) — 최신 세법·증권사 요율 확인 필요\n")

    L.append("## C 전략 기준점(그룹 방식, 기준 기간 2005~2018 상위 30/15/5%)\n")
    L.append("| 대상 | 제외 신호 | 1/2/3단계 기준점 | 기준 기간 실제 비율 | n(일) |\n|---|---|---|---|---|")
    for tk, d in data["targets"].items():
        if d is None:
            L.append(f"| {tk} | - | 데이터 없음 | - | - |")
            continue
        th = d["thresholds"]
        thr = " / ".join("도달 불가" if v == float("inf") else f"{v:.1f}" for v in th.values)
        L.append(f"| {tk} | {', '.join(cfg['exclude_signals'].get(tk, [])) or '없음'} | {thr} | "
                 f"{' / '.join(f'{a:.1%}' for a in th.actual_shares)} | {th.n_days} |")

    L.append(f"\n## 전체 기간 결과 ({start} ~ {end})\n")
    L.append("MDD·최장 회복 기간은 현금흐름을 뺀 시간가중 지수 기준. B의 연환산은 돈의 가중(XIRR)이라 A의 시간가중 연환산과 정의가 "
             "다르다. ▼ = 연환산이 같은 대상의 A보다 낮음. C는 2018년 이전이 기준점을 정한 표본 안 구간이다.\n")
    L.append("| 대상 | 전략 | 납입 | 기말 평가액 | 누적 | 연환산 | MDD | 최장 회복(일) | 세후 누적 | 세후 연환산 | 거래 | 비고 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    full = {}
    for tk, d in data["targets"].items():
        if d is None:
            continue
        res = {s: run_one(d, s, start, cfg, end) for s in mc.STRATEGIES}
        full[tk] = res
        a_cagr = res["A"][1].cagr
        for s, (r, m) in res.items():
            mark = " ▼" if s != "A" and m.cagr < a_cagr else ""
            rec = f"{m.recovery_days}{'(미회복)' if m.unrecovered else ''}"
            L.append(f"| {tk} | {mc.STRATEGY_LABELS[s]} | {m.contributed:,.0f} | {m.final_value:,.0f} | {_p(m.cum)} | "
                     f"{_p(m.cagr)}{mark} | {_p(m.mdd)} | {rec} | {_p(m.after_tax_cum)} | {_p(m.after_tax_cagr)} | "
                     f"{r.trades} | {'; '.join(r.notes)} |")
    # 해외 대상 현지 통화 참고(A만)
    L.append("\n원화 환산 효과 참고 — 해외 대상 A를 달러 그대로 계산했을 때(환율 영향 제외):\n")
    L.append("| 대상 | 누적(USD) | 연환산(USD) | MDD(USD) | 누적(원화) | 연환산(원화) | MDD(원화) |\n|---|---|---|---|---|---|---|")
    for tk in cfg["usd_targets"]:
        d = data["targets"].get(tk)
        if d is None:
            continue
        ohlc = store.get_ohlc(tk).sort_index()
        m_ = (ohlc.index >= pd.Timestamp(start)) & (ohlc.index <= pd.Timestamp(end))
        o_usd = ohlc["open"][m_].astype(float)
        c_usd = ohlc["close"][m_].astype(float)
        usd_costs = mc.MarketCosts(d["costs"].commission, lambda x: 0.0)
        r = mc.simulate("A", o_usd, c_usd, usd_costs, initial=1.0, monthly=0.0)
        mu = mc.metrics("A", r)
        mk = full[tk]["A"][1]
        L.append(f"| {tk} | {_p(mu.cum)} | {_p(mu.cagr)} | {_p(mu.mdd)} | {_p(mk.cum)} | {_p(mk.cagr)} | {_p(mk.mdd)} |")

    # 시작 연도별 분포
    years = sorted({d.year for d in pd.date_range(start, end, freq="YS")} | {pd.Timestamp(start).year})
    years = [y for y in years if pd.Timestamp(f"{y}-12-31") < pd.Timestamp(end)]
    L.append(f"\n## 시작 연도별 결과 분포 (각 연도 첫 거래일 시작 → {end}, 연환산)\n")
    L.append("| 대상 | 전략 | 시작 연도 수 | 최소 | 중앙값 | 최대 | A보다 낮은 연도 수 |\n|---|---|---|---|---|---|---|")
    per_year_rows = []
    for tk, d in data["targets"].items():
        if d is None:
            continue
        cagr = {s: [] for s in mc.STRATEGIES}
        for y in years:
            s0 = max(pd.Timestamp(start), pd.Timestamp(f"{y}-01-01")).date().isoformat()
            row = {s: run_one(d, s, s0, cfg, end)[1].cagr for s in mc.STRATEGIES}
            for s in mc.STRATEGIES:
                cagr[s].append(row[s])
            per_year_rows.append((tk, y, row))
        for s in mc.STRATEGIES:
            arr = np.array(cagr[s], float)
            below = "-" if s == "A" else str(int(sum(1 for a, b in zip(cagr[s], cagr["A"]) if a < b)))
            L.append(f"| {tk} | {mc.STRATEGY_LABELS[s]} | {len(arr)} | {_p(np.nanmin(arr))} | {_p(np.nanmedian(arr))} | "
                     f"{_p(np.nanmax(arr))} | {below} |")
    L.append("\n### 시작 연도별 연환산(전체)\n")
    L.append("| 대상 | 시작 | " + " | ".join(mc.STRATEGY_LABELS[s] for s in mc.STRATEGIES) + " |")
    L.append("|---|---|" + "---|" * len(mc.STRATEGIES))
    for tk, y, row in per_year_rows:
        cells = [f"{_p(row[s])}{' ▼' if s != 'A' and row[s] < row['A'] else ''}" for s in mc.STRATEGIES]
        L.append(f"| {tk} | {y}{' (C 표본 안 포함)' if y <= 2018 else ''} | " + " | ".join(cells) + " |")
    L.append("\n과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. 어느 시장·전략이 낫다는 결론이 아니며 매수·매도 추천이 아닙니다.")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="국장/미장 비교 백테스트(원화 기준)")
    p.add_argument("--config", default=str(CONFIG))
    p.add_argument("--end", default=None, help="기본: 네 대상 공통 마지막 거래일")
    p.add_argument("--out", default=None, help="마크다운으로 저장할 경로(기본: 화면 출력만)")
    a = p.parse_args(argv)
    store.init_db()
    text = report(load_config(Path(a.config)), a.end)
    print(text)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
