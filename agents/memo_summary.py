"""
막스 메모 요약 — 사이클 관점의 참고 자료로만 쓴다. **개인 참고용이며 외부에 공개하지 않는다**(README).

본문은 호출하는 쪽(scripts/run_debate.py --execute)이 그때 받아 넘기고, 여기서는 메모리에서만 쓴다(저장 안 함).
요약은 한국어로 짧게(600자 이내), 원문 문장을 옮겨 적지 않는다(CLAUDE.md 원칙 5). 원문과 40자 이상 똑같은 구간이
있거나, 인물 1인칭 인용·예측 금지 문구가 있으면 요약을 버린다(None) — 저장하지 않는다.
anthropic 패키지를 import하지 않는다(debate.py와 같은 덕 타이핑 client).
"""

from __future__ import annotations

from agents.debate import DebateConfig, _create
from agents.orchestrator import _extract_text, _get, _scan_for_violations

MAX_CHARS = 600
VERBATIM_WINDOW = 40

SYSTEM = ("개인 참고용으로 투자 메모를 한국어로 요약합니다. 규칙: 4문장 이내, 600자 이내. 원문 문장을 그대로 옮기거나 "
          "따옴표로 인용하지 않는다. 저자의 1인칭 발언처럼 쓰지 않고 '메모는 ~를 다룬다'처럼 문서를 주어로 쓴다. "
          "향후 주가 방향 예측이나 매매 권유를 덧붙이지 않는다.")


def verbatim_overlap(summary: str, body: str, window: int = VERBATIM_WINDOW) -> bool:
    s = " ".join(summary.split())
    b = " ".join(body.split())
    return any(s[i:i + window] in b for i in range(0, max(0, len(s) - window + 1)))


def summarize(client, cfg: DebateConfig, title: str, body: str) -> tuple[str | None, list[str], tuple[int, int]]:
    """(요약 또는 None, 문제 목록, (입력 토큰, 출력 토큰))."""
    resp = _create(client, DebateConfig(cfg.model, cfg.effort, 800, cfg.refusal_fallback), SYSTEM,
                   f"제목: {title}\n\n본문:\n{body}")
    usage = _get(resp, "usage")
    tokens = ((_get(usage, "input_tokens", 0) or 0), (_get(usage, "output_tokens", 0) or 0)) if usage else (0, 0)
    text = _extract_text(_get(resp, "content", []) or [])
    problems = [f"금지 문구 '{p}'" for p in _scan_for_violations(text)]
    if _get(resp, "stop_reason") in ("refusal", "max_tokens") or not text:
        problems.append(f"요약 응답 비정상({_get(resp, 'stop_reason')})")
    if len(text) > MAX_CHARS:
        problems.append(f"요약이 {MAX_CHARS}자 초과")
    if text and verbatim_overlap(text, body):
        problems.append(f"원문과 {VERBATIM_WINDOW}자 이상 같은 구간 — 인용으로 보고 버림")
    return (None if problems else text), problems, tokens
