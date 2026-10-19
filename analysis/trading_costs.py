"""
거래비용 파라미터 — 수수료율·증권거래세율·슬리피지.

**세율은 연도별로 여러 번 바뀌었다**(국내 증시 매도 시 부과되는 증권거래세+
농어촌특별세). 이 파일은 "날짜별로 다른 세율을 적용할 수 있는 구조"
(`TaxRateSchedule`)만 만들어둔다 — **정확한 시행일·요율 숫자는 지어내지
않는다**(공통 규칙). 기본으로 들어있는 값은 기존에 이 프로젝트가 이미
쓰고 있던 근사치(`analysis/rule_backtest.DEFAULT_TAX_RATE = 0.0020`) 하나뿐
이고, 그 이전/이후의 실제 변동 이력은 사용자가 국세청·한국거래소 공고로
확인한 뒤 `TAX_RATE_SCHEDULE`에 직접 추가해야 한다(TODO로 남겨둠).

슬리피지(slippage): 호가 체결 시 의도한 가격과 실제 체결가의 차이 근사치.
기본값 0 — 이 프로젝트는 실제 체결 데이터가 없어서 실측하지 않았고, 추정치를
지어내는 대신 0으로 비워뒀다(기본 "비용 없음" 취급). 필요하면 사용자가
직접 값을 넣는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

DEFAULT_COMMISSION_RATE = 0.00015  # 편도 0.015% — 참고용 근사치(증권사·상품별로 다름, 확인 필요)
DEFAULT_SLIPPAGE_RATE = 0.0  # 기본 0 — 실측 데이터 없어 지어내지 않음. 필요시 사용자가 설정.


@dataclass(frozen=True)
class RateChange:
    effective_on: date
    tax_rate: float
    source_note: str  # 출처·확인 상태. "TODO: 미확인"이면 숫자를 믿지 말 것.


# 참고용 시작 테이블. 이 프로젝트가 이미 쓰던 근사치 하나만 넣어뒀다 —
# 과거 변동 이력(예: 2023년·2025년 인하 등)을 지어내서 채우지 않았다.
# TODO(사용자): 국세청/한국거래소 공고로 실제 시행일·요율을 확인해서
# 이 리스트에 RateChange를 추가할 것 (effective_on 오름차순으로).
TAX_RATE_SCHEDULE: list[RateChange] = [
    RateChange(
        effective_on=date(2000, 1, 1),
        tax_rate=0.0020,
        source_note="analysis/rule_backtest.DEFAULT_TAX_RATE 기존값을 그대로 가져옴 — "
                     "실제 시행일·정확한 요율 TODO: 미확인, 사용자가 확인 후 교체할 것.",
    ),
]


def tax_rate_on(d: date, schedule: list[RateChange] | None = None) -> float:
    """
    해당 날짜에 적용되는 세율. schedule에서 effective_on <= d인 가장 최근
    항목을 쓴다. schedule을 안 주면 모듈 기본 TAX_RATE_SCHEDULE을 쓴다.
    schedule이 비어 있거나 d보다 이른 항목이 하나도 없으면 가장 이른
    항목의 값을 그대로 쓴다(과거로 외삽 — 더 나은 정보가 없다는 뜻).
    """
    schedule = schedule if schedule is not None else TAX_RATE_SCHEDULE
    if not schedule:
        return 0.0
    applicable = [r for r in schedule if r.effective_on <= d]
    if not applicable:
        return min(schedule, key=lambda r: r.effective_on).tax_rate
    return max(applicable, key=lambda r: r.effective_on).tax_rate
