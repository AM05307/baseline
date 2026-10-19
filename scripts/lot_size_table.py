"""
주문 단위 점검표 — 단계별 목표 비중(정상·1단계·2단계·3단계)이 운용 자본 안에서 실제로 몇 주, 몇 %로 실현되는지.

정수 주식(국내 개별주·ETF)은 1주 단위 내림, 소수점 매매(해외 주식 소수점 주문 가정)는 --fractional-unit 단위.
가격은 baseline.db 최신 종가를 읽는다(읽기만). DB에 없으면 --price로 직접 넣어야 하고, 없으면 "데이터 없음"으로 둔다
(가격을 지어내지 않는다). 해외 종목 가격은 USD로 넣으면 DB의 USDKRW(최신)로 원화 환산한다.

사용:
    python scripts/lot_size_table.py
    python scripts/lot_size_table.py --capital 1000000 --price 069500=40000 --price SPY=600 --price QQQ=520
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.position_policy import PositionPolicy, realized_weight  # noqa: E402
from db import store  # noqa: E402

STAGES = [("정상", 1.0), ("1단계", 0.66), ("2단계", 0.33), ("3단계", 0.0)]
COMMISSION = 0.00015
USD_TICKERS = {"SPY", "QQQ"}
FRACTIONAL = {"SPY", "QQQ"}


def live_1_capital(config_dir: Path = Path(__file__).resolve().parent.parent / "config") -> tuple[Path | None, float | None]:
    """config/capital_tiers.local.yaml → .example.yaml 순서로 live_1 한도를 읽는다(executor/config_files.resolve_config와
    같은 순서 — baseline 쪽은 executor를 import하지 않으므로 YAML을 직접 읽는다)."""
    import yaml
    for name in ("capital_tiers.local.yaml", "capital_tiers.example.yaml", "capital_tiers.yaml"):
        f = config_dir / name
        if f.exists():
            tiers = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("tiers", [])
            v = next((t.get("max_capital_krw") for t in tiers if t.get("name") == "live_1"), None)
            return f, (float(v) if isinstance(v, (int, float)) else None)
    return None, None


def table(ticker: str, price_krw: float, capital: float, fractional_unit: float | None) -> list[dict]:
    rows = []
    for name, w in STAGES:
        if fractional_unit:
            unit_price = price_krw * fractional_unit
            q, real = realized_weight(w, unit_price, capital, PositionPolicy(buy_cost_rate=COMMISSION))
            qty = q * fractional_unit
        else:
            qty, real = realized_weight(w, price_krw, capital, PositionPolicy(buy_cost_rate=COMMISSION))
        rows.append({"stage": name, "target": w, "qty": qty, "realized": real, "gap_pctp": (w - real) * 100})
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="단계별 목표 비중의 실현 수량·비중(주문 단위 점검)")
    p.add_argument("--capital", type=float, default=None,
                   help="운용 자본(원). 기본: 설정 파일(config/capital_tiers.local.yaml, 없으면 example)의 live_1 한도")
    p.add_argument("--tickers", default="000660,069500,SPY,QQQ")
    p.add_argument("--price", action="append", default=[], help="TICKER=가격(국내 원, 해외 USD). DB 값보다 우선")
    p.add_argument("--fractional-unit", type=float, default=0.0001, help="소수점 매매 최소 수량(가정, 기본 0.0001주)")
    p.add_argument("--warn-pctp", type=float, default=10.0)
    args = p.parse_args(argv)
    if args.capital is None:  # 실제 한도는 local 설정 파일에만 있다(공개 저장소에는 예시값)
        path, cap = live_1_capital()
        if not cap:
            p.error(f"{path.name if path else 'capital_tiers 설정'}의 live_1 한도가 없음/미확정 — --capital로 직접 넣으세요")
        args.capital = float(cap)
        print(f"(운용 자본 기본값: {path.name}의 live_1 한도{' — 예시값' if path.name.endswith('.example.yaml') else ''})")
    store.init_db()
    manual = {k: float(v) for k, v in (x.split("=", 1) for x in args.price)}
    fx = store.get_prices("USDKRW")
    usdkrw = float(fx.iloc[-1]) if not fx.empty else None
    print(f"운용 자본 {args.capital:,.0f}원 · 수수료 {COMMISSION:.3%} 반영 · USDKRW "
          f"{f'{usdkrw:,.2f}' if usdkrw else '데이터 없음'} · 경고 기준 {args.warn_pctp:g}%p")
    for t in [x.strip() for x in args.tickers.split(",") if x.strip()]:
        src = "직접 입력(가정)" if t in manual else "baseline.db 최신 종가"
        if t in manual:
            price = manual[t]
        else:
            s = store.get_prices(t)
            price = float(s.iloc[-1]) if not s.empty else None
            if price is not None:
                src += f" {s.index[-1].date()}"
        if price is None:
            print(f"\n■ {t}: 가격 데이터 없음 — 수집 후 다시 실행하거나 --price {t}=가격")
            continue
        if t in USD_TICKERS:
            if usdkrw is None:
                print(f"\n■ {t}: 환율 데이터 없음")
                continue
            price_krw, label = price * usdkrw, f"${price:,.2f} × {usdkrw:,.2f} = {price * usdkrw:,.0f}원"
        else:
            price_krw, label = price, f"{price:,.0f}원"
        frac = args.fractional_unit if t in FRACTIONAL else None
        print(f"\n■ {t} ({label}, {src}{', 소수점 ' + str(frac) + '주 단위 가정' if frac else ', 1주 단위'})")
        print(f"  {'단계':6s} {'목표':>6s} {'수량':>10s} {'실현':>7s} {'차이':>8s}")
        for r in table(t, price_krw, args.capital, frac):
            qty = f"{r['qty']:.4f}주" if frac else f"{int(r['qty'])}주"
            warn = "  ⚠" if abs(r["gap_pctp"]) > args.warn_pctp else ""
            print(f"  {r['stage']:6s} {r['target']:>6.0%} {qty:>10s} {r['realized']:>7.1%} {r['gap_pctp']:>+7.1f}%p{warn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
