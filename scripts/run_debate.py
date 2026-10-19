"""
관점별 토론 실행 — **기본은 dry-run**(전송할 프롬프트와 예상 토큰·비용만 출력, API 호출 없음).
실제 호출은 `--execute`를 붙였을 때만. 자동 스케줄에 넣지 않는다(README) — 사람이 직접 돌린다.

결과는 섀도 기록(debate_shadow)만 — 매매 결정·위험 점수에 반영하지 않는다. 가드레일에 걸리면 저장하지 않고
검토 대기(debate_review)로 보낸다. 과거 날짜 실행 옵션은 없다: 결정 기준일은 항상 DB의 마지막 종가일이고, 그 데이터가
max_snapshot_age_days보다 오래됐으면 실행하지 않는다(agents/debate.py docstring — 과거 백테스트 오염).

실행 전 차단(dry-run에서도 같은 판정을 보여줌):
  - 같은 (기준일, 스냅샷 해시, 모델)이 이미 기록됨 → 다시 돌리지 않음(멱등)
  - 이번 실행 최악 토큰(입력 추정 + 호출 수 × max_tokens_per_call) > max_tokens_per_run
  - 이번 달 토론 비용 + 이번 실행 최악 비용 > monthly_budget_usd

사용:
    python scripts/run_debate.py                 # dry-run
    python scripts/run_debate.py --show-prompts  # dry-run + 1라운드 프롬프트 전문
    python scripts/run_debate.py --execute       # 실제 호출(비용 발생)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from agents import debate  # noqa: E402
from db import store  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "debate.yaml"
MEMO_SUMMARY_TOKENS = (12000, 800)  # 메모 요약 1회 최악(입력 본문 추정, 출력 상한) — 요약이 필요할 때만 더한다


def load_config(path: Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def debate_config(cfg: dict) -> debate.DebateConfig:
    return debate.DebateConfig(model=cfg.get("model", "claude-opus-5-5"), effort=cfg.get("effort", "medium"),
                               max_tokens_per_call=int(cfg.get("max_tokens_per_call", 2500)),
                               refusal_fallback=bool(cfg.get("refusal_fallback", True)))


@dataclass
class Budget:
    ok: bool
    reasons: list[str]
    worst_in: int
    worst_out: int
    worst_cost: float
    month_cost: float


def cost(cfg: dict, tokens_in: int, tokens_out: int) -> float:
    price = cfg.get("price_per_mtok", {})
    return tokens_in / 1e6 * float(price.get("input", 0)) + tokens_out / 1e6 * float(price.get("output", 0))


def check_budget(cfg: dict, worst_in: int, worst_out: int, spent_in: int, spent_out: int) -> Budget:
    reasons = []
    total = worst_in + worst_out
    if total > int(cfg.get("max_tokens_per_run", 0)):
        reasons.append(f"1회 토큰 상한 초과: 최악 {total:,} > {int(cfg.get('max_tokens_per_run', 0)):,}")
    wc, mc = cost(cfg, worst_in, worst_out), cost(cfg, spent_in, spent_out)
    if mc + wc > float(cfg.get("monthly_budget_usd", 0)):
        reasons.append(f"월 예산 초과: 이번 달 ${mc:.2f} + 이번 실행 최악 ${wc:.2f} > ${float(cfg.get('monthly_budget_usd', 0)):.2f}")
    return Budget(not reasons, reasons, worst_in, worst_out, wc, mc)


def main(argv: list[str] | None = None, *, client_factory=None, snapshot_fn=debate.build_snapshot,
         memo_text_fn=None, today: date | None = None, cfg: dict | None = None, path=None) -> int:
    p = argparse.ArgumentParser(description="관점별 토론(섀도 기록) — 기본 dry-run")
    p.add_argument("--execute", action="store_true", help="실제 API 호출(비용 발생). 없으면 dry-run")
    p.add_argument("--show-prompts", action="store_true", help="dry-run에서 1라운드 프롬프트 전문 출력")
    args = p.parse_args(argv)
    cfg = cfg or load_config()
    dcfg = debate_config(cfg)
    today = today or date.today()
    store.init_db(path)

    snap = snapshot_fn()
    if not snap.decision_date:
        print("000660 시세가 없어 기준일을 정할 수 없습니다 — collectors/collect_daily.py 먼저.")
        return 1
    age = (today - date.fromisoformat(snap.decision_date)).days
    if age > int(cfg.get("max_snapshot_age_days", 5)) or age < 0:
        print(f"기준일 {snap.decision_date} 데이터가 {age}일 지남 — 앞으로만 기록하므로 실행하지 않습니다(먼저 수집).")
        return 1
    if store.debate_shadow_exists(snap.decision_date, snap.hash, dcfg.model, path):
        print(f"이미 기록됨: 기준일 {snap.decision_date} · 스냅샷 {snap.hash[:12]} · {dcfg.model} — 다시 실행하지 않습니다.")
        return 0

    memo = store.latest_memo(path)
    needs_summary = bool(memo) and not memo.get("summary")
    worst_in, worst_out, calls = debate.worst_case_tokens(snap, memo, dcfg)
    if needs_summary:
        worst_in, worst_out, calls = worst_in + MEMO_SUMMARY_TOKENS[0], worst_out + MEMO_SUMMARY_TOKENS[1], calls + 1
    month_start = today.replace(day=1).isoformat()
    spent_in, spent_out = store.debate_tokens_since(month_start, path)
    budget = check_budget(cfg, worst_in, worst_out, spent_in, spent_out)

    print(f"[run_debate] {'EXECUTE' if args.execute else 'DRY-RUN'} · 모델 {dcfg.model} (effort {dcfg.effort}, "
          f"거절 대비 fallback {'켬' if dcfg.refusal_fallback else '끔'})")
    print(f"기준일 {snap.decision_date} · 스냅샷 sha256 {snap.hash[:12]} · 키 {list(snap.data)}")
    print(f"호출 {calls}회(1라운드 {sum(1 for x in debate.PERSPECTIVES if x.round1)} · 2라운드 {len(debate.PERSPECTIVES)} · "
          f"종합 1{' · 메모 요약 1' if needs_summary else ''})")
    print(f"예상 토큰(최악): 입력 ~{worst_in:,} · 출력 최대 {worst_out:,} → 최악 비용 ${budget.worst_cost:.2f} "
          f"(이번 달 사용 ${budget.month_cost:.2f} / 예산 ${float(cfg.get('monthly_budget_usd', 0)):.2f})")
    print("※ 입력 토큰은 글자 수 기반 추정치 — 실제 값은 응답 usage로 기록된다.")
    for c in debate.plan_first_round(snap, memo):
        print(f"\n--- 1라운드 · {c.perspective} ({debate.estimate_tokens(c.system + c.user):,} 토큰 추정) ---")
        if args.show_prompts:
            print(c.system + "\n\n" + c.user)
    print("\n2라운드·종합 프롬프트는 1라운드 응답이 들어가므로 실행 시점에 만들어진다(형식은 agents/debate.py).")
    if not budget.ok:
        print("\n실행하지 않음 — " + " / ".join(budget.reasons))
        return 3
    if not args.execute:
        print("\nDRY-RUN: API를 호출하지 않았습니다. 실제 실행은 --execute.")
        return 0

    if client_factory is None:
        import anthropic
        client_factory = anthropic.Anthropic
    client = client_factory()
    extra_in = extra_out = 0
    if needs_summary:
        from agents import memo_summary
        from collectors import marks_memos
        body = (memo_text_fn or marks_memos.fetch_memo_text)(memo["url"])
        if body:
            summary, problems, (ti, to) = memo_summary.summarize(client, dcfg, memo["title"], body)
            extra_in, extra_out = ti, to
            if summary:
                store.set_memo_summary(memo["url"], summary, dcfg.model, path=path)
                memo = store.latest_memo(path)
            else:
                print("메모 요약을 버림: " + " / ".join(problems))
            del body  # 본문은 저장하지 않는다
        else:
            print("메모 본문을 받을 수 없음(이용약관·robots 확인 미통과 등) — 사이클 관점은 '요약 없음'으로 진행")

    res = debate.run_debate(client, snap, memo, dcfg)
    res.input_tokens += extra_in
    res.output_tokens += extra_out
    if res.passed:
        saved = store.insert_debate_shadow(debate.shadow_row(res, memo), path=path)
        print(f"\n저장: 결론 '{res.conclusion}' · 근거 {res.evidence} · 토큰 {res.input_tokens:,}/{res.output_tokens:,}"
              + ("" if saved else " (이미 있던 기록 — 새로 저장 안 함)"))
        print("섀도 기록입니다 — 매매 결정·위험 점수에 반영되지 않습니다.")
        return 0
    store.insert_debate_review(debate.review_row(res), path=path)
    print("\n가드레일 위반 — debate_shadow에 저장하지 않고 검토 대기(debate_review)로 보냈습니다:")
    for w in res.warnings:
        print(f"  - {w}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
