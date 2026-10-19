"""
LLM 에이전트 오케스트레이터.

mcp_server/tools.py가 이미 MCP 클라이언트(Claude Desktop/Code)에 노출하는 9개
조회 전용 툴을, 여기서는 **같은 파이썬 함수 그대로** Anthropic Messages API의
tool-use 루프에 연결한다. MCP 와이어 프로토콜(stdio)을 다시 구현하지 않고
tools.py 함수를 직접 호출하는 이유는 이게 우리 프로젝트 안에서 도는 배치
스크립트이기 때문이다 — 외부 MCP 클라이언트가 필요로 하는 건 이미
mcp_server/server.py가 담당한다.

**이 모듈은 anthropic 패키지를 import하지 않는다.** `analyze_ticker()`가 받는
`client`는 `.messages.create(...)`만 있으면 되는 아무 객체다(덕 타이핑). 실제
운영에서는 `anthropic.Anthropic()` 인스턴스를 넘기고(scripts/run_agents.py),
테스트에서는 가짜 클라이언트를 넘겨서 이 파일의 툴 호출 루프 로직만 anthropic
패키지 설치 없이 검증한다.

가드레일 (CLAUDE.md 원칙 1·2·3·5 그대로 적용):
  1. 방향 예측 금지 — 확률/목표가로 완곡하게 표현하는 것도 금지.
  2. 매매 조언 금지.
  3. 표본 수 30 미만이면 반드시 함께 알릴 것.
  5. 실존 인물의 1인칭 발언을 만들지 않음 — 프레임워크 이름만 라벨로 허용.

시스템 프롬프트로 강제하는 것만으로는 부족할 수 있다는 전제 하에, 응답 텍스트를
금지 문구 목록으로 다시 한번 훑는 `_scan_for_violations()`를 추가 안전장치로
둔다(defense in depth). 위반이 발견되면 `AgentAnalysis.warnings`에 담기고,
scripts/run_agents.py는 이 경우 DB에 저장하지 않고 사람이 직접 검토하게 한다.

CLAUDE.md 원칙 1은 "백테스트로 검증된 규칙만 매수/매도 기준가로 표시한다"로
갱신됐지만(analysis/price_rules.py·price_rule_backtest.py, web/main.py의
"매수·목표·손절 기준가" 카드), **이 예외는 순전히 수식으로 계산되고 엄격한
통과 기준(거래 100건↑·전체·최근1년 거래당평균 둘 다 양수)을 거친 숫자에만
적용된다.** 이 파일(LLM이 생성하는 서술)은 그 예외 대상이 아니다 — 여기 금지
문구 목록과 `_scan_for_violations()`는 그대로 전부 유지한다. LLM이 "매수
시점"·목표가·확률 같은 서술을 만들어내는 것과, 코드가 백테스트를 통과한 규칙의
가격을 기계적으로 출력하는 것은 완전히 다른 신뢰 수준이기 때문이다.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import tools  # noqa: E402

HYNIX = tools.HYNIX
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_TOOL_ITERATIONS = 6

SYSTEM_PROMPT = """당신은 'baseline' 프로젝트의 리서치 보조 에이전트입니다. 사용자 한 명만
보는 개인용 도구이며 공개 서비스가 아닙니다. 아래 규칙을 예외 없이 지키세요.

1. 향후 주가 방향을 예측하지 마세요. "오를 것이다"/"내릴 것이다"는 물론,
   확률이나 목표가로 완곡하게 표현하는 것도 금지입니다. 과거·현재 관측값만
   서술하세요.
2. 매수·매도 시점, 주문 관련 조언을 하지 마세요.
3. 표본 수(samples/n)가 30 미만인 수치를 인용할 때는 반드시 "표본이 적어
   해석에 주의가 필요하다"는 사실을 함께 언급하세요. 숨기거나 축소하지 마세요.
4. 실존 인물(예: 워런 버핏, 피터 린치)의 1인칭 발언을 만들어내지 마세요.
   "~가 말하길", "~은 이렇게 본다" 같은 인용 형태를 쓰지 마세요. "그레이엄·
   버핏 프레임워크"처럼 프레임워크 이름을 라벨로만 언급하는 것은 괜찮습니다.
5. 툴이 반환한 값 외의 사실을 지어내지 마세요. 재무제표(PER·PBR·EPS 등)처럼
   툴로 조회할 수 없는 데이터는 "확인할 수 없다"고 명시하고, 학습된 지식으로
   추측해서 채우지 마세요.
6. 분석 대상 종목에 대해 최소 한 번은 get_framework_check 툴을 호출해서 이미
   충족/미충족/확인 필요로 분류된 사실을 근거로 삼으세요. 필요하면 get_news,
   get_market_sentiment, get_adr_premium, get_backtest 등으로 맥락을 보강해도
   됩니다.
7. 마지막 답변은 한국어 평서문 산문으로 쓰세요. "사세요/파세요"가 아니라
   "이 프레임워크 기준에서 지금 걸리는 부분은 X, 확인이 안 되는 부분은 Y"
   형식으로, 어떤 원칙에서 무엇이 걸리고 무엇이 확인되지 않았는지를 정리하는
   것이 목적입니다."""

USER_PROMPT_TEMPLATE = (
    "오늘은 {today}입니다. 티커 {ticker}에 대해 리서치 요약을 작성해줘. "
    "get_framework_check을 최소 한 번 호출하고, 필요하면 다른 툴로 맥락을 "
    "보강한 뒤, 지금 데이터에서 확인되는 것과 확인되지 않는 것을 자연스러운 "
    "한국어 문단으로 정리해줘. 방향 예측이나 매매 조언은 절대 하지 마."
)

# Anthropic Messages API tool-use 스펙. mcp_server/tools.py 함수 시그니처·
# docstring과 내용을 맞춰뒀다 — 같은 함수를 감싸는 두 스펙(MCP 프로토콜용은
# 함수 시그니처에서 자동 생성, 이건 수동)이 서로 어긋나지 않도록 관리 필요.
TOOL_SPECS = [
    {
        "name": "get_correlation",
        "description": "두 종목 일간수익률의 상관계수·베타·최대상관 시차를 계산한다. 방향 판단은 하지 않는다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "대상 종목 (예: NVDA, MU, AMD, TSM, 000660)"},
                "benchmark": {"type": "string", "description": "비교 기준 종목", "default": "SOX"},
                "window": {"type": "integer", "description": "최근 거래일 수", "default": 60},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_divergence_days",
        "description": "평소 상관관계에서 벗어난 날짜를 찾는다. 원인은 제공하지 않는다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "대상 종목"},
                "benchmark": {"type": "string", "description": "비교 기준 종목", "default": "SOX"},
                "window": {"type": "integer", "description": "베타 계산 기간(거래일)", "default": 120},
                "threshold": {"type": "number", "description": "표준편차 배수 임계값", "default": 2.0},
                "limit": {"type": "integer", "description": "최대 반환 개수", "default": 10},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_calendar_events",
        "description": "앞으로 N일 안의 만기일·자사주매입 기간·실적 발표 등 확정된 일정만 반환한다. 전망은 포함하지 않는다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "오늘부터 며칠 뒤까지", "default": 30},
            },
        },
    },
    {
        "name": "get_backtest",
        "description": "캘린더 이벤트 기준 상대일 구간의 과거 수익률 분포. 표본 30개 미만이면 reliable=False.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "대상 종목"},
                "event": {"type": "string", "description": "현재는 quad_witching만 지원", "default": "quad_witching"},
                "entry_offset": {"type": "integer", "default": -10},
                "exit_offset": {"type": "integer", "default": -1},
                "years_back": {"type": "integer", "default": 3},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_adr_premium",
        "description": "SK하이닉스 ADR(SKHY)과 KRX 보통주(000660) 사이의 가격 차이(프리미엄)를 계산한다. 매매 신호가 아니다.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_market_sentiment",
        "description": "VIX 기반 시장 심리 게이지. CNN Fear&Greed Index와는 다른 자체 대체 지표.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_news",
        "description": "저장된 뉴스 헤드라인. confidence='rumor'는 미확인 소문이므로 사실처럼 전달하면 안 된다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "특정 종목만 (생략 시 전체)"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "get_framework_check",
        "description": (
            "투자 프레임워크 렌즈 체크(리스크·사이클·팩트체커·가치·성장·밸류에이션 6개). "
            "실존 인물의 발언을 인용하지 않고, 지금 데이터로 확인 가능한 사실만 "
            "충족/미충족/확인 필요로 분류해 반환한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "default": HYNIX},
                "benchmark": {"type": "string", "default": "SOX"},
                "event": {"type": "string", "default": "quad_witching"},
                "years_back": {"type": "integer", "default": 3},
            },
        },
    },
]

_TOOL_DISPATCH = {
    "get_correlation": tools.get_correlation,
    "get_divergence_days": tools.get_divergence_days,
    "get_calendar_events": tools.get_calendar_events,
    "get_backtest": tools.get_backtest,
    "get_adr_premium": tools.get_adr_premium,
    "get_market_sentiment": tools.get_market_sentiment,
    "get_news": tools.get_news,
    "get_framework_check": tools.get_framework_check,
}

# 시스템 프롬프트로 막아도 모델이 어길 가능성에 대비한 2차 방어선.
# 걸리면 저장을 막고 사람이 검토하게 한다 — scripts/run_agents.py 참고.
BANNED_PHRASES = [
    "오를 것", "내릴 것", "오를 겁니다", "내릴 겁니다", "오를 전망", "내릴 전망",
    "상승할 것", "하락할 것", "매수 시점", "매도 시점", "지금 사세요", "지금 파세요",
    "매수 추천", "매도 추천", "매수하세요", "매도하세요",
    "가 말하길", "이 말하길", "가 말했다", "이 말했다",
    "가 이렇게 말한다", "이 이렇게 말한다", "은 이렇게 본다", "는 이렇게 본다",
    "buy now", "sell now", "will rise", "will fall",
]


class DataNotFoundError(ValueError):
    """tools 모듈과 동일한 이름 재노출 — orchestrator 사용자가 mcp_server를 몰라도 되게."""


def _run_tool(name: str, arguments: dict) -> dict:
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"알 수 없는 툴: {name}"}
    try:
        return fn(**arguments)
    except tools.DataNotFoundError as e:
        return {"error": str(e)}
    except ValueError as e:
        return {"error": str(e)}


def _scan_for_violations(text: str) -> list[str]:
    return [phrase for phrase in BANNED_PHRASES if phrase in text]


def _get(block, key: str, default=None):
    """anthropic SDK 객체(속성 접근)와 테스트용 dict(키 접근) 양쪽 다 지원."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _extract_text(content_blocks) -> str:
    parts = [_get(b, "text", "") for b in content_blocks if _get(b, "type") == "text"]
    return "\n".join(p for p in parts if p).strip()


def _extract_tool_uses(content_blocks) -> list[dict]:
    return [
        {"id": _get(b, "id"), "name": _get(b, "name"), "input": _get(b, "input") or {}}
        for b in content_blocks
        if _get(b, "type") == "tool_use"
    ]


@dataclass
class AgentAnalysis:
    ticker: str
    model: str
    narrative: str
    tool_calls: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def analyze_ticker(
    client,
    ticker: str = HYNIX,
    model: str = DEFAULT_MODEL,
    max_tool_iterations: int = MAX_TOOL_ITERATIONS,
    today: date | None = None,
) -> AgentAnalysis:
    """
    tool-use 루프를 돌려서 ticker에 대한 자연어 리서치 요약을 만든다.

    client: `.messages.create(model=, system=, tools=, max_tokens=, messages=)`를
    제공하는 아무 객체. 실제 운영: `anthropic.Anthropic()`. 테스트: 가짜 클라이언트.
    """
    today = today or date.today()
    messages: list[dict] = [{
        "role": "user",
        "content": USER_PROMPT_TEMPLATE.format(ticker=ticker, today=today.isoformat()),
    }]

    tool_call_log: list[dict] = []
    usage_in = usage_out = 0
    iterations = 0
    narrative = ""

    while iterations < max_tool_iterations:
        iterations += 1
        response = client.messages.create(
            model=model, system=SYSTEM_PROMPT, tools=TOOL_SPECS,
            max_tokens=1500, messages=messages,
        )
        usage = _get(response, "usage")
        if usage is not None:
            usage_in += _get(usage, "input_tokens", 0) or 0
            usage_out += _get(usage, "output_tokens", 0) or 0

        content = _get(response, "content", [])
        stop_reason = _get(response, "stop_reason")
        tool_uses = _extract_tool_uses(content)

        if stop_reason != "tool_use" or not tool_uses:
            narrative = _extract_text(content)
            break

        messages.append({"role": "assistant", "content": content})
        tool_result_blocks = []
        for use in tool_uses:
            result = _run_tool(use["name"], use["input"])
            tool_call_log.append({"name": use["name"], "arguments": use["input"], "result": result})
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": use["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })
        messages.append({"role": "user", "content": tool_result_blocks})
    else:
        narrative = narrative or "(최대 반복 횟수에 도달해 응답을 완결하지 못했습니다. 검토가 필요합니다.)"

    warnings = _scan_for_violations(narrative)
    if not tool_call_log:
        warnings.append("어떤 툴도 호출하지 않고 답변함 — 근거 데이터 없이 작성됐을 가능성이 있음")

    return AgentAnalysis(
        ticker=ticker, model=model, narrative=narrative,
        tool_calls=tool_call_log, warnings=warnings, iterations=iterations,
        input_tokens=usage_in, output_tokens=usage_out,
    )
