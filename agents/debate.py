"""
관점별 토론 — 같은 데이터 스냅샷을 여러 관점이 보고 의견을 낸 뒤(1라운드) 서로 반박하고(2라운드) 종합자가 정리한다.
결과는 **섀도 기록만** 한다(db: debate_shadow). 매매 결정·위험 점수·상태 문구에 절대 반영하지 않는다 — executor는 이
기록을 읽지도 import하지도 않고(tests/test_debate.py 정적 검사), 위험 점수 계산 코드도 debate를 참조하지 않는다.

**앞으로만 기록한다.** 과거 날짜로 토론을 돌리는 기능은 일부러 만들지 않았다. 언어 모델은 과거 시장에서 실제로 무슨 일이
있었는지를 학습했을 수 있어서, 과거 날짜 스냅샷을 주고 토론시키면 "그때 알 수 없던 결과"가 판단에 섞인다(미래참조 오염).
그래서 결정 기준일은 항상 지금 DB의 마지막 종가일이고(scripts/run_debate.py가 너무 오래된 데이터면 실행 거부), 채점
(scripts/debate_scorecard.py)은 기록이 앞으로 쌓인 뒤에만 의미가 있다.

관점(실존 인물 이름을 쓰지 않는다 — 관점 이름만 라벨, CLAUDE.md "멀티 에이전트 설계"):
  사이클 / 금리·유동성 / 가치 / 추세 / 반대(다른 관점의 결론을 반박하는 역할)
최신 막스 메모 요약은 사이클 관점에만 참고 자료로 준다(요약은 개인 참고용 — 원문 인용 없음, 인물 1인칭 발언 금지).

같은 스냅샷: 토론 시작 전에 mcp_server/tools.py 조회 함수들을 한 번 불러 스냅샷(JSON)을 만들고 sha256 해시를 남긴다.
모든 관점은 이 같은 JSON만 받는다(관점마다 툴을 따로 부르게 하면 서로 다른 데이터를 볼 수 있어서). 위험 점수·상태는
스냅샷에 넣지 않았다 — 토론이 모델 결과를 그대로 따라가지 않게 하고, 채점에서 "위험 점수 상태와의 일치율"을 독립적으로
보려는 것.

출력: 종합 결론은 "위험 높음 / 중립 / 위험 낮음" 3단계와 근거 데이터(스냅샷 키) 목록뿐. 매수·매도 지시, 목표가·확률 예측 금지.
가드레일: agents/orchestrator._scan_for_violations()(예측·매매 지시·실존 인물 1인칭 인용 금지 문구)를 모든 의견·종합에
다시 적용하고, 결론 값·근거 키·응답 잘림·거절도 확인한다. 하나라도 걸리면 debate_shadow에 저장하지 않고 검토 대기
(debate_review)로 보낸다.

이 모듈은 anthropic 패키지를 import하지 않는다. client는 `.messages.create(...)`(테스트용 가짜) 또는
`.beta.messages.create(...)`(실제 anthropic.Anthropic — 거절 대비 서버 측 fallback 베타를 켤 때)를 가진 객체면 된다.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.orchestrator import _extract_text, _get, _scan_for_violations  # noqa: E402
from mcp_server import tools  # noqa: E402

HYNIX = tools.HYNIX
CONCLUSIONS = ("위험 높음", "중립", "위험 낮음")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class Perspective:
    key: str
    label: str
    focus: str
    round1: bool = True  # 반대 관점은 1라운드에서 의견을 내지 않고 2라운드에서 반박만 한다


PERSPECTIVES = [
    Perspective("cycle", "사이클 관점", "시장 사이클의 위치, 투자자 심리, 위험 선호의 정도"),
    Perspective("rates", "금리·유동성 관점", "금리 사이클 단계, 금리 수준과 변화, 유동성 여건"),
    Perspective("value", "가치 관점", "밸류에이션과 안전마진(데이터가 없으면 없다고 말한다)"),
    Perspective("trend", "추세 관점", "가격 추세와 모멘텀, 이동평균·고점 대비 위치"),
    Perspective("contrarian", "반대 관점", "다른 관점들의 결론에서 근거가 약한 부분을 찾아 반박", round1=False),
]

# 스냅샷: (키, 조회 함수, 인자). 전부 읽기 전용 조회 — mcp_server/tools.py.
SNAPSHOT_SOURCES = [
    ("price_000660", "get_price_summary", {"ticker": HYNIX}),
    ("rates", "get_rates_summary", {}),
    ("market_sentiment", "get_market_sentiment", {}),
    ("correlation_sox", "get_correlation", {"ticker": HYNIX, "benchmark": "SOX"}),
    ("adr_premium", "get_adr_premium", {}),
    ("framework_check", "get_framework_check", {"ticker": HYNIX}),
    ("calendar_30d", "get_calendar_events", {"days": 30}),
    ("news_000660", "get_news", {"ticker": HYNIX, "limit": 10}),
]

RULES = """규칙(예외 없음):
1. 향후 주가 방향을 예측하지 않는다. "오를 것/내릴 것", 확률, 목표가로 완곡하게 표현하는 것도 금지. 관측값만 해석한다.
2. 매수·매도·비중 조절 같은 행동을 지시하거나 권하지 않는다.
3. 실존 인물의 1인칭 발언이나 "~가 말하길/~은 이렇게 본다" 같은 인용을 만들지 않는다. 메모 요약은 "메모 요약에 따르면"처럼
   문서로만 언급한다.
4. 스냅샷 JSON에 없는 사실을 지어내지 않는다. 값이 없거나 error면 "데이터 없음"이라고 쓴다. samples가 30 미만이면 표본이
   적다고 함께 쓴다.
5. 근거를 댈 때는 스냅샷의 최상위 키 이름(예: price_000660, rates)을 함께 적는다.
6. 한국어 평서문, 8문장 이내."""


@dataclass
class Snapshot:
    data: dict
    json_text: str
    hash: str
    decision_date: str | None


def build_snapshot() -> Snapshot:
    data = {}
    for key, fn_name, kwargs in SNAPSHOT_SOURCES:
        try:
            data[key] = getattr(tools, fn_name)(**kwargs)
        except (tools.DataNotFoundError, ValueError) as e:
            data[key] = {"error": str(e)}
    return snapshot_from_data(data)


def snapshot_from_data(data: dict) -> Snapshot:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    price = data.get("price_000660") or {}
    return Snapshot(data, text, hashlib.sha256(text.encode("utf-8")).hexdigest(), price.get("as_of"))


@dataclass
class DebateConfig:
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_tokens_per_call: int = 2500
    refusal_fallback: bool = True


@dataclass
class PlannedCall:
    stage: str  # 'round1' | 'round2' | 'synthesis'
    perspective: str
    system: str
    user: str


def _system(p: Perspective) -> str:
    return (f"당신은 개인용 리서치 도구 'baseline'의 토론 참가자 '{p.label}'입니다. 초점: {p.focus}.\n"
            "SK하이닉스(000660)에 대해 아래 데이터 스냅샷만 보고, 지금 관측되는 위험의 정도를 이 관점에서 설명합니다.\n"
            + RULES)


SYNTH_SYSTEM = ("당신은 토론 종합자입니다. 각 관점의 의견과 반박을 읽고 지금 관측되는 위험의 정도를 하나로 정리합니다.\n"
                + RULES + "\n7. 마지막에 아래 형식의 JSON 한 덩어리만 출력한다(코드 블록 없이):\n"
                '{"conclusion": "위험 높음|중립|위험 낮음 중 하나", "evidence": ["스냅샷 최상위 키", ...], '
                '"summary": "3문장 이내 근거 요약"}')


def _user_round1(p: Perspective, snap: Snapshot, memo: dict | None) -> str:
    memo_part = ""
    if p.key == "cycle":
        if memo and memo.get("summary"):
            memo_part = (f"\n\n[참고 자료 — 최신 오크트리 메모 요약(개인 참고용 요약, 원문 아님)] "
                         f"{memo.get('title')} ({memo.get('published_on') or '게시일 데이터 없음'}): {memo['summary']}")
        else:
            memo_part = "\n\n[참고 자료] 최신 메모 요약: 데이터 없음"
    return f"[데이터 스냅샷 sha256={snap.hash[:12]}]\n{snap.json_text}{memo_part}\n\n1라운드: 이 관점의 의견을 쓰세요."


def _user_round2(p: Perspective, snap: Snapshot, round1: dict[str, str]) -> str:
    others = "\n\n".join(f"[{_label(k)}]\n{v}" for k, v in round1.items() if k != p.key)
    task = ("다른 관점들의 결론에서 근거가 약하거나 데이터와 맞지 않는 부분을 반박하세요." if p.key == "contrarian"
            else "다른 관점들의 의견 중 동의하지 않는 부분을 데이터 근거로 반박하고, 필요하면 자기 의견을 고치세요.")
    return f"[데이터 스냅샷 sha256={snap.hash[:12]}]\n{snap.json_text}\n\n[1라운드 의견]\n{others}\n\n2라운드: {task}"


def _user_synthesis(snap: Snapshot, round1: dict[str, str], round2: dict[str, str]) -> str:
    r1 = "\n\n".join(f"[{_label(k)} · 1라운드]\n{v}" for k, v in round1.items())
    r2 = "\n\n".join(f"[{_label(k)} · 2라운드]\n{v}" for k, v in round2.items())
    return f"[데이터 스냅샷 sha256={snap.hash[:12]}]\n{snap.json_text}\n\n{r1}\n\n{r2}\n\n종합하세요."


def _label(key: str) -> str:
    return next(p.label for p in PERSPECTIVES if p.key == key)


def estimate_tokens(text: str) -> int:
    """호출 없이 쓰는 보수적 추정(한국어·JSON 혼합 — 글자 2개당 1토큰). 실제 값은 count_tokens/응답 usage로 확인."""
    return math.ceil(len(text) / 2)


def plan_first_round(snap: Snapshot, memo: dict | None) -> list[PlannedCall]:
    return [PlannedCall("round1", p.key, _system(p), _user_round1(p, snap, memo)) for p in PERSPECTIVES if p.round1]


def worst_case_tokens(snap: Snapshot, memo: dict | None, cfg: DebateConfig) -> tuple[int, int, int]:
    """(입력 토큰 추정, 출력 토큰 최대, 호출 수). 2라운드·종합 입력은 앞 라운드 출력이 최대치라고 가정해서 더한다."""
    r1 = plan_first_round(snap, memo)
    n1 = len(r1)
    n2 = len(PERSPECTIVES)
    calls = n1 + n2 + 1
    snap_tokens = estimate_tokens(snap.json_text)
    base_sys = estimate_tokens(_system(PERSPECTIVES[0]))
    inp = sum(estimate_tokens(c.system + c.user) for c in r1)
    inp += n2 * (base_sys + snap_tokens + (n1 - 1) * cfg.max_tokens_per_call + 200)
    inp += estimate_tokens(SYNTH_SYSTEM) + snap_tokens + (n1 + n2) * cfg.max_tokens_per_call + 200
    return inp, calls * cfg.max_tokens_per_call, calls


@dataclass
class DebateResult:
    snapshot: Snapshot
    model: str
    model_served: str | None = None
    round1: dict[str, str] = field(default_factory=dict)
    round2: dict[str, str] = field(default_factory=dict)
    synthesis_text: str = ""
    conclusion: str | None = None
    evidence: list[str] = field(default_factory=list)
    summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    warnings: list[str] = field(default_factory=list)
    calls: int = 0

    @property
    def passed(self) -> bool:
        return not self.warnings


def _create(client, cfg: DebateConfig, system: str, user: str):
    kwargs = dict(model=cfg.model, max_tokens=cfg.max_tokens_per_call, system=system,
                  messages=[{"role": "user", "content": user}], output_config={"effort": cfg.effort})
    beta = getattr(client, "beta", None)
    if cfg.refusal_fallback and beta is not None and hasattr(beta, "messages"):
        return beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    return client.messages.create(**kwargs)


def _ask(client, cfg: DebateConfig, res: DebateResult, where: str, system: str, user: str) -> str:
    resp = _create(client, cfg, system, user)
    res.calls += 1
    usage = _get(resp, "usage")
    if usage is not None:
        res.input_tokens += _get(usage, "input_tokens", 0) or 0
        res.output_tokens += _get(usage, "output_tokens", 0) or 0
    res.model_served = _get(resp, "model", None) or res.model_served
    stop = _get(resp, "stop_reason")
    text = _extract_text(_get(resp, "content", []) or [])
    if stop == "refusal":
        res.warnings.append(f"{where}: 모델이 응답을 거절함(refusal)")
    elif stop == "max_tokens":
        res.warnings.append(f"{where}: 출력 상한(max_tokens)에 걸려 잘림")
    if not text:
        res.warnings.append(f"{where}: 빈 응답")
    for phrase in _scan_for_violations(text):
        res.warnings.append(f"{where}: 금지 문구 '{phrase}'")
    return text


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_synthesis(text: str, snap: Snapshot) -> tuple[str | None, list[str], str, list[str]]:
    """(결론, 근거 키, 요약, 문제 목록). 결론이 3단계 밖이거나 근거 키가 스냅샷에 없으면 문제로 적는다."""
    problems = []
    m = _JSON_RE.findall(text or "")
    try:
        obj = json.loads(m[-1]) if m else None
    except json.JSONDecodeError:
        obj = None
    if not isinstance(obj, dict):
        return None, [], "", ["종합: JSON 결론을 찾지 못함"]
    conclusion = obj.get("conclusion")
    if conclusion not in CONCLUSIONS:
        problems.append(f"종합: 결론 값이 3단계가 아님({conclusion!r})")
        conclusion = None
    evidence = obj.get("evidence") if isinstance(obj.get("evidence"), list) else []
    bad = [k for k in evidence if k not in snap.data]
    if not evidence:
        problems.append("종합: 근거 데이터 목록이 비어 있음")
    if bad:
        problems.append(f"종합: 스냅샷에 없는 근거 키 {bad}")
    return conclusion, [k for k in evidence if k in snap.data], str(obj.get("summary", "")), problems


def run_debate(client, snap: Snapshot, memo: dict | None, cfg: DebateConfig) -> DebateResult:
    res = DebateResult(snapshot=snap, model=cfg.model)
    for call in plan_first_round(snap, memo):
        res.round1[call.perspective] = _ask(client, cfg, res, f"1라운드·{_label(call.perspective)}", call.system, call.user)
    for p in PERSPECTIVES:
        res.round2[p.key] = _ask(client, cfg, res, f"2라운드·{p.label}", _system(p), _user_round2(p, snap, res.round1))
    res.synthesis_text = _ask(client, cfg, res, "종합", SYNTH_SYSTEM, _user_synthesis(snap, res.round1, res.round2))
    res.conclusion, res.evidence, res.summary, problems = parse_synthesis(res.synthesis_text, snap)
    res.warnings.extend(problems)
    return res


def shadow_row(res: DebateResult, memo: dict | None) -> dict:
    return {
        "decision_date": res.snapshot.decision_date, "snapshot_hash": res.snapshot.hash, "model": res.model,
        "model_served": res.model_served,
        "opinions": json.dumps({"round1": res.round1, "round2": res.round2}, ensure_ascii=False),
        "conclusion": res.conclusion, "evidence": json.dumps(res.evidence, ensure_ascii=False),
        "synthesis": res.summary, "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
        "guardrail": json.dumps({"passed": True, "checked": ["banned_phrases", "conclusion_value", "evidence_keys",
                                                             "refusal", "truncation"]}, ensure_ascii=False),
        "memo_url": (memo or {}).get("url"),
    }


def review_row(res: DebateResult) -> dict:
    return {
        "decision_date": res.snapshot.decision_date or date.today().isoformat(), "snapshot_hash": res.snapshot.hash,
        "model": res.model,
        "payload": json.dumps({"round1": res.round1, "round2": res.round2, "synthesis": res.synthesis_text},
                              ensure_ascii=False),
        "warnings": json.dumps(res.warnings, ensure_ascii=False),
        "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
    }
