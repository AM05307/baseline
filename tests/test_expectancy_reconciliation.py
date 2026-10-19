"""
2단계(백테스트 기대값 검산): "기대값 = 승률×평균수익 − 패률×평균손실"로
다시 계산해도 같은 값이 나오는지 확인.

결론(코드 변경 없음, 검산만): RuleBacktestResult.mean_return은
sum(trades)/n으로 직접 계산되는데, 이는 수학적으로
win_rate_frac*avg_win + loss_rate_frac*avg_loss와 항상 같다 — win_rate가
"t>0"을, avg_loss가 "t<=0"을 기준으로 승/패를 가르는 게 서로 어긋나지
않게(0은 항상 패로) 정의돼 있어서 두 부분집합이 전체를 정확히 둘로
나눈다(원문: "중앙값을 승리 폭처럼 쓰는 계산"은 찾아보니 없었다 — 중앙값은
median_return_pct로 평균 옆에 참고용으로만 같이 표시되고, 기대값 계산에는
안 쓰인다). 그래도 이 동치를 명시적인 회귀 테스트로 박아둔다 — 나중에
avg_win/avg_loss/win_rate의 승/패 경계 정의가 서로 어긋나게 바뀌면 바로
깨지도록.
"""

from __future__ import annotations

import pytest

from analysis import rule_backtest as rbt


def _reconstructed_expectancy(result: rbt.RuleBacktestResult) -> float:
    """승률×평균수익(승리 거래) − 패률×평균손실(손실 거래)로 재구성."""
    if result.n == 0:
        return float("nan")
    win_frac = result.win_rate / 100
    loss_frac = 1 - win_frac
    avg_win = result.avg_win if result.avg_win == result.avg_win else 0.0
    avg_loss = result.avg_loss if result.avg_loss == result.avg_loss else 0.0
    return win_frac * avg_win + loss_frac * avg_loss


@pytest.mark.parametrize("trades", [
    [1.0, 2.0, -5.0],
    [1.0, -1.0],
    [5.0, 5.0, 5.0],
    [-1.0, -2.0, -3.0],
    [0.0, 1.0, -1.0],  # 정확히 0인 거래(패로 분류됨, win_rate와 avg_loss 경계 일치 확인)
    [10.0, -3.0, -3.0, -3.0, 2.0, 2.0, -50.0],
])
def test_mean_return_equals_win_rate_weighted_reconstruction(trades):
    result = rbt.RuleBacktestResult(label="t", trades=trades)
    assert result.mean_return == pytest.approx(_reconstructed_expectancy(result))


def test_empty_trades_both_nan():
    result = rbt.RuleBacktestResult(label="t", trades=[])
    assert result.mean_return != result.mean_return  # NaN
    assert _reconstructed_expectancy(result) != _reconstructed_expectancy(result)  # NaN


def test_median_is_not_used_as_a_win_size_substitute():
    # median_return_pct(있는 모듈: pullback.RegimeBreakdown)는 평균(기대값)과
    # 별개의 통계로만 존재해야 한다 — 평균 계산식 안에 median이 섞여 있으면
    # 안 된다. 극단치 하나가 median과 mean을 크게 갈라놓는 사례로 확인.
    trades = [1.0] * 29 + [-100.0]  # 중앙값은 +1.0, 평균(기대값)은 크게 음수여야 함
    result = rbt.RuleBacktestResult(label="t", trades=trades)
    assert result.mean_return < 0  # 극단적 손실 거래가 기대값에 제대로 반영됨
    assert result.mean_return == pytest.approx(_reconstructed_expectancy(result))
