"""
baseline MCP 서버.

analysis/ 모듈을 감싼 조회 전용 MCP 툴을 노출한다. 로직 자체는
mcp_server/tools.py에 있고, 여기는 FastMCP 배선만 한다.

주의 (CLAUDE.md 원칙 2): 이 서버에는 매매/주문 툴을 추가하지 않는다.
KIS API의 주문 엔드포인트는 kis_client.py에도 의도적으로 구현돼 있지 않다.

실행 (프로젝트 루트에서):
    python -m mcp_server.server

Claude Desktop에 연결하려면 claude_desktop_config.json에 다음을 추가:
    {
      "mcpServers": {
        "baseline": {
          "command": "python",
          "args": ["-m", "mcp_server.server"],
          "cwd": "/절대/경로/baseline"
        }
      }
    }

Claude Code CLI로 등록:
    claude mcp add baseline -- python -m mcp_server.server
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from mcp_server import tools  # noqa: E402

INSTRUCTIONS = """
이 서버는 주식 시장 데이터의 관측값(상관계수·베타·이벤트/규칙 백테스트·캘린더
이벤트)만 제공합니다.

이 서버를 쓰는 모든 응답에서 반드시 지킬 것:
1. 이 서버가 반환한 수치를 근거로 "오를 것이다", "지금이 매수 시점이다" 같은
   방향성 판단을 만들어내지 마세요. 숫자는 관측치 그대로 전달하세요.
   get_rule_backtest도 마찬가지입니다 — 과거 규칙 성과 측정이지 매매 신호가 아닙니다.
2. 응답에 samples/n 필드가 있으면, 그 값이 30 미만일 때 반드시 표본이 적다는
   사실을 사용자에게 함께 알리세요. 숨기거나 축소하지 마세요.
3. 이 서버에는 매수/매도/주문 기능이 없습니다. 사용자가 매매를 요청해도
   이 서버로는 수행할 수 없다고 안내하세요.
4. (참고) CLAUDE.md 원칙 1은 "백테스트로 검증된 규칙만 매수/매도 기준가로
   표시한다"로 갱신됐습니다 — 단, 그 예외는 대시보드(web/main.py)가
   analysis/price_rule_backtest.py의 엄격한 통과 기준(거래 100건↑·전체·
   최근1년 거래당평균 둘 다 양수)을 거쳐 기계적으로 계산한 가격에만
   적용되고, 이 서버는 아직 그 기능을 툴로 노출하지 않습니다. 이 서버의
   숫자를 근거로 LLM이 직접 "매수 시점"을 판단해 말하는 건 여전히 금지입니다
   (1번 그대로).
""".strip()

mcp = FastMCP("baseline", instructions=INSTRUCTIONS)

# tools.py의 순수 함수를 그대로 툴로 등록한다.
# 함수 시그니처의 타입 힌트와 docstring이 그대로 MCP 툴 스키마/설명이 된다.
mcp.tool()(tools.get_correlation)
mcp.tool()(tools.get_divergence_days)
mcp.tool()(tools.get_calendar_events)
mcp.tool()(tools.get_backtest)
mcp.tool()(tools.get_collection_status)
mcp.tool()(tools.get_adr_premium)
mcp.tool()(tools.get_market_sentiment)
mcp.tool()(tools.get_news)
mcp.tool()(tools.get_framework_check)
mcp.tool()(tools.get_agent_analysis)
mcp.tool()(tools.get_claim_scoreboard)
mcp.tool()(tools.get_rule_backtest)
mcp.tool()(tools.get_price_summary)
mcp.tool()(tools.get_rates_summary)


if __name__ == "__main__":
    mcp.run()
