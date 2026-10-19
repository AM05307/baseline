"""
pullback 전략(기본 파라미터)이 왜 거의 -99%로 수렴하는지 분해한다.

세 가지 후보 원인을 나눠서 본다:
    1) 거래비용 효과 — 비용을 0으로 뒀을 때와 비교
    2) 복리(compounding) 효과 — 같은 거래 수익률 리스트를 단순 합산했을
       때와 복리로 이어 곱했을 때의 차이
    3) 상위 손실 거래 10건의 기여도 — 그 10건만 빼고 다시 복리로 계산

**거래 목록(진입/청산 날짜) 자체는 비용과 무관하게 동일하다** — 진입가·
목표가·손절가는 가격 조건만으로 정해지고, 수수료/세금/슬리피지는 체결가에만
곱해지는 값이라 "언제 사고 파는가"에는 영향을 주지 않는다(analysis/
pullback.py의 체결 로직 참고). 그래서 비용 0 버전도 똑같은 거래 수로
나온다 — 숫자만 다르다.

주문/매매는 하지 않는다(원칙 2). 출력에 예측 문구를 넣지 않는다(원칙 1).

사용:
    python scripts/decompose_pullback.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analysis import pullback as pb  # noqa: E402
from db import store  # noqa: E402

HYNIX = "000660"
START_DATE = "2005-01-01"


def _compound(returns_pct: list[float]) -> float:
    cum = 1.0
    for r in returns_pct:
        cum *= 1 + r / 100
    return (cum - 1) * 100


def _simple_sum(returns_pct: list[float]) -> float:
    return sum(returns_pct)


def main() -> None:
    store.init_db()
    ohlc = store.get_ohlc(HYNIX)
    if ohlc.empty:
        print(f"[{HYNIX}] 데이터가 없습니다. 먼저 수집하세요: python collectors/kr_market.py max")
        sys.exit(1)
    ohlc = ohlc.loc[ohlc.index >= pd.Timestamp(START_DATE)]
    if len(ohlc) < 250:
        print(f"[{HYNIX}] {START_DATE} 이후 데이터가 {len(ohlc)}일뿐입니다.")
        sys.exit(1)

    trades_actual = pb.simulate_pullback_trades(ohlc)  # 기본 파라미터(비용 포함, DEFAULT_*)
    trades_nocost = pb.simulate_pullback_trades(ohlc, commission_rate=0, tax_rate=0, slippage_rate=0)

    n_actual, n_nocost = len(trades_actual), len(trades_nocost)
    print(f"[{HYNIX}] {START_DATE} ~ {ohlc.index[-1].date()} · pullback 기본 파라미터")
    print(f"거래 수: 비용 포함 {n_actual}건 / 비용 0 {n_nocost}건"
          f"{' (일치 — 예상대로 거래 목록은 비용과 무관)' if n_actual == n_nocost else ' ⚠ 불일치(원인 재확인 필요)'}")

    r_actual = [t.return_pct for t in trades_actual]
    r_nocost = [t.return_pct for t in trades_nocost]

    compounded_actual = _compound(r_actual)
    compounded_nocost = _compound(r_nocost)
    simple_sum_actual = _simple_sum(r_actual)

    print("\n1) 거래비용 효과")
    print(f"   복리 총수익률(비용 포함, 실제)  : {compounded_actual:+.2f}%")
    print(f"   복리 총수익률(비용 0)           : {compounded_nocost:+.2f}%")
    print(f"   -> 비용 효과(차이)              : {compounded_actual - compounded_nocost:+.2f}%p")

    print("\n2) 복리(compounding) 효과")
    print(f"   단순 합산(비복리, 비용 포함)    : {simple_sum_actual:+.2f}%")
    print(f"   복리 총수익률(비용 포함)        : {compounded_actual:+.2f}%")
    print(f"   -> 복리 효과(차이)              : {compounded_actual - simple_sum_actual:+.2f}%p "
          f"(음수가 크면 '작은 손실이 반복 복리로 눈덩이처럼 커졌다'는 뜻)")

    worst10 = sorted(r_actual)[:10]
    rest = sorted(r_actual)[10:]
    compounded_without_worst10 = _compound(rest)
    print("\n3) 상위 손실 거래 10건의 기여도")
    print(f"   최악 10건 수익률: {[f'{r:+.2f}%' for r in worst10]}")
    print(f"   그 10건 제외하고 복리 계산       : {compounded_without_worst10:+.2f}%")
    print(f"   전체(비용 포함) 복리 총수익률    : {compounded_actual:+.2f}%")
    print(f"   -> 최악 10건 제외 시 차이        : {compounded_without_worst10 - compounded_actual:+.2f}%p")

    print(
        "\n결론: 위 세 값 중 가장 큰 (양의) 차이가 주된 원인이다 — 숫자로 직접 "
        "비교해서 판단할 것(이 스크립트는 판단하지 않고 분해된 값만 보여준다)."
    )
    print(
        "\n과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. "
        "이 결과는 매수·매도 추천이 아닙니다."
    )


if __name__ == "__main__":
    main()
