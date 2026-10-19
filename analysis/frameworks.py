"""
투자 프레임워크 렌즈.

CLAUDE.md의 "멀티 에이전트 설계"를 개인용으로 축소해서 먼저 구현한 것.
실존 인물을 흉내내지 않는다 — "워런 버핏이 오늘 이렇게 말한다" 같은 1인칭
발언을 만들지 않는다. 대신 그 사람들이 공개적으로 알려진 프레임워크
(안전마진, PEG, 사이클 포지셔닝 등)의 이름만 빌려서, 지금 있는 데이터로
"이 기준에서 무엇이 걸리는가"만 충족/미충족/확인 필요로 분류한다.

**정직하게 밝혀둘 것**: 이 프로젝트는 재무제표(PER·PBR·EPS·부채비율 등)를
아직 수집하지 않는다. 그래서 가치·성장·밸류에이션처럼 재무데이터가 필요한
렌즈는 진짜로 계산할 수 없다 — 그럴듯하게 지어내는 대신 "확인 필요"로
정직하게 비워둔다. 리스크·사이클·팩트체커는 지금 있는 가격·상관관계·
캘린더·백테스트 데이터만으로 실제로 계산 가능해서 구현했다.

analysis/ 관례대로 순수 함수만 담는다. DB나 API를 import하지 않는다 —
호출하는 쪽(web/main.py, mcp_server/tools.py)이 이미 계산해둔 값을 인자로 넘긴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATUS_MET = "충족"
STATUS_UNMET = "미충족"
STATUS_UNKNOWN = "확인 필요"


@dataclass
class LensFinding:
    criterion: str
    status: str      # STATUS_MET | STATUS_UNMET | STATUS_UNKNOWN
    detail: str       # 근거 — 숫자를 그대로 서술, 방향 해석 없이


@dataclass
class LensResult:
    lens: str
    findings: list[LensFinding] = field(default_factory=list)
    data_available: bool = True
    note: str = ""


def risk_lens(
    backtest_n: int, backtest_reliable: bool, backtest_worst_pct: float | None,
    adr_premium_pct: float | None,
) -> LensResult:
    """
    리스크 관점: 최대 손실 시나리오와 구조적 갭 리스크를 확인한다.
    '위험하다/안전하다' 같은 판단은 하지 않는다 — 수치가 있는지, 표본이
    충분한지, 구조적으로 갭이 존재하는지만 본다.
    """
    findings = []

    if backtest_n == 0:
        findings.append(LensFinding(
            "이벤트 백테스트 최대손실 데이터", STATUS_UNKNOWN,
            "해당 이벤트 구간에 백테스트할 과거 데이터가 없음",
        ))
    else:
        findings.append(LensFinding(
            "이벤트 백테스트 표본이 통계적으로 충분한가 (n≥30)",
            STATUS_MET if backtest_reliable else STATUS_UNMET,
            f"표본 {backtest_n}회" + ("" if backtest_reliable else " — 30회 미만, 우연일 가능성 배제 못함"),
        ))
        if backtest_worst_pct is not None:
            findings.append(LensFinding(
                "과거 최대손실 수치가 기록돼 있는가", STATUS_MET,
                f"최대손실 {backtest_worst_pct:+.2f}% (거래비용 미반영, 과거 관측치)",
            ))

    if adr_premium_pct is None:
        findings.append(LensFinding(
            "ADR-KRX 구조적 갭 존재 여부", STATUS_UNKNOWN,
            "ADR 프리미엄 데이터 없음",
        ))
    else:
        has_gap = abs(adr_premium_pct) >= 2.0
        findings.append(LensFinding(
            "ADR-KRX 가격 갭이 유의미한 크기로 존재하는가 (|프리미엄|≥2%)",
            STATUS_MET if has_gap else STATUS_UNMET,
            f"현재 프리미엄 {adr_premium_pct:+.2f}% — 갭이 좁혀지거나 벌어질 때 "
            f"어느 한쪽에 변동성이 생길 수 있는 구조적 요인 (방향은 알 수 없음)",
        ))

    return LensResult(lens="리스크 (최대 손실 시나리오)", findings=findings)


def cycle_lens(
    has_active_period_event: bool, upcoming_earnings_within_days: int | None,
    beta_vs_benchmark: float | None, correlation_reliable: bool,
) -> LensResult:
    """
    사이클 관점: 지금이 확정된 이벤트 구간(자사주매입 등) 안인지, 업종
    베타가 높은 종목인지를 본다. '사이클의 어느 지점인가'라는 판단(피크/바닥
    추정)은 재고·가동률 데이터가 있어야 가능해서 여기선 하지 않는다.
    """
    findings = [
        LensFinding(
            "현재 공시 기반 확정 이벤트 구간(자사주매입 등) 안에 있는가",
            STATUS_MET if has_active_period_event else STATUS_UNMET,
            "오늘 날짜가 등록된 기간형 이벤트에 포함됨" if has_active_period_event
            else "오늘 해당하는 기간형 이벤트 없음",
        ),
    ]

    if upcoming_earnings_within_days is None:
        findings.append(LensFinding(
            "임박한 실적 발표가 있는가", STATUS_UNKNOWN, "등록된 실적 일정 없음",
        ))
    else:
        soon = upcoming_earnings_within_days <= 7
        findings.append(LensFinding(
            "실적 발표가 7일 이내로 임박했는가",
            STATUS_MET if soon else STATUS_UNMET,
            f"D-{upcoming_earnings_within_days}",
        ))

    if beta_vs_benchmark is None or not correlation_reliable:
        findings.append(LensFinding(
            "업종 베타가 사이클 민감도를 판단할 만큼 신뢰 가능한가",
            STATUS_UNKNOWN, "표본 부족 또는 데이터 없음",
        ))
    else:
        findings.append(LensFinding(
            "업종(SOX) 대비 베타가 1.0을 넘는가 (사이클 진폭이 벤치마크보다 큰가)",
            STATUS_MET if beta_vs_benchmark > 1.0 else STATUS_UNMET,
            f"베타 {beta_vs_benchmark:+.2f}",
        ))

    findings.append(LensFinding(
        "지금이 사이클의 어느 국면인지(피크/바닥)",
        STATUS_UNKNOWN,
        "재고·가동률·설비투자 데이터가 없어 판단 불가 — 향후 확장 지점",
    ))

    return LensResult(lens="사이클 (반도체 업황 포지셔닝)", findings=findings)


def fact_checker_lens(cited: list[dict]) -> LensResult:
    """
    다른 렌즈들이 인용한 수치가 다 계산됐고, 표본 정보가 붙어 있는지 확인한다.
    DB를 다시 조회해서 대조하는 게 아니라 — analysis/ 모듈은 DB를 모르므로 —
    "화면에 나가는 숫자에 구멍이 없는가"를 검사하는 완결성 체크에 가깝다.

    cited: [{"label": str, "value": float|None, "samples": int|None}, ...]
    """
    findings = []
    for item in cited:
        label = item.get("label", "(라벨 없음)")
        value = item.get("value")
        samples = item.get("samples")

        if value is None or value != value:  # None 또는 NaN
            findings.append(LensFinding(
                f"'{label}' 값이 계산되었는가", STATUS_UNKNOWN, "데이터 없음 (아직 계산되지 않음)",
            ))
            continue

        if samples is not None and samples < 30:
            findings.append(LensFinding(
                f"'{label}' 표본이 인용하기에 충분한가", STATUS_UNMET,
                f"표본 {samples}개 — 다른 렌즈에서 이 수치를 인용할 때도 표본 부족 경고 필요",
            ))
        else:
            findings.append(LensFinding(
                f"'{label}' 값이 계산되었고 표본 표기가 있는가", STATUS_MET,
                f"값 {value:.4g}" + (f", 표본 {samples}개" if samples is not None else ""),
            ))

    return LensResult(lens="팩트체커 (인용 수치 완결성 검증)", findings=findings)


def unavailable_lens(name: str, reason: str) -> LensResult:
    """가치·성장·밸류에이션처럼 지금 데이터로 계산할 수 없는 렌즈의 정직한 스텁."""
    return LensResult(
        lens=name,
        findings=[LensFinding("데이터 가용성", STATUS_UNKNOWN, reason)],
        data_available=False,
        note=reason,
    )


# 아직 재무데이터가 없어서 계산 불가 — 그럴듯하게 지어내지 않고 명시적으로 비워둔다.
VALUE_LENS_STUB = lambda: unavailable_lens(  # noqa: E731
    "가치 (안전마진 · 해자) — 그레이엄·버핏 프레임워크",
    "PER/PBR/부채비율 등 재무제표 데이터 소스가 없음. KIS 연동 또는 별도 공시 API 필요.",
)
GROWTH_LENS_STUB = lambda: unavailable_lens(  # noqa: E731
    "성장 (PEG · 성장 지속성) — 린치 프레임워크",
    "EPS 성장률 등 실적 데이터 소스가 없음. 분기 실적 데이터 수집 필요.",
)
VALUATION_LENS_STUB = lambda: unavailable_lens(  # noqa: E731
    "밸류에이션 (내재 가정 역산)",
    "현재가에 내재된 성장률을 역산하려면 DCF에 필요한 재무 가정 데이터가 있어야 함.",
)
