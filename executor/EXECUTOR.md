# EXECUTOR.md — 실행 계층 원칙과 안전장치

`executor/`는 baseline(측정·대시보드)과 분리된 실행 계층이다. 이 문서는 executor에만 적용되고,
CLAUDE.md의 원칙(특히 원칙 2 "매매 기능을 구현하지 않는다", 보안 원칙 6 "KIS 주문 API 호출 금지")은
**문구 그대로 유지**된다. 두 문서가 충돌하면 CLAUDE.md가 우선한다.

## 0. 범위 결정 (2026-10-18)

- 사용자 결정: **모의투자 주문까지만** 구현 범위. 실제 자금으로 주문하는 브로커 코드(실전 URL·실전 tr_id)는
  만들지 않는다. 브로커 구현체는 `FakeBroker`(테스트용)·`ShadowBroker`(섀도 원장 안 체결)·`KisBroker`(**모의투자 전용**
  골격, 2026-10-18)다. KisBroker에는 실전 URL로 보내는 경로와 실전 tr_id가 없다.
- 이번 세션까지 **실제 주문은 한 번도 보내지 않았다**. executor 코드는 `.env`를 읽지 않는다.
- `live_*` 단계는 자본 사다리의 한도·이력 체계로만 존재한다. 실제 자금 주문 경로를 열려면 CLAUDE.md
  원칙 갱신(아래 7절 안)을 사용자가 먼저 확정해야 한다.

## 1. 의존 방향

- executor → baseline: `analysis/`·`db/`만, **읽기 전용**으로 import한다(`db.store`는 조회 함수만).
- baseline → executor: **금지**. `analysis·db·collectors·web·scripts·mcp_server·agents·config.py` 어디에서도
  executor를 import하지 않는다. 그래서 executor CLI는 `scripts/`가 아니라 `executor/scripts/`에 있고
  (`python -m executor.scripts.<이름>`), "자금 배분 현황" 화면도 baseline 대시보드 카드가 아니라 executor 자체
  페이지다(`uvicorn executor.web:app --port 8001`).
- executor 데이터는 `executor.db`(저장소 루트, `*.db`로 gitignore)에 따로 둔다. baseline.db에는 쓰지 않는다.
- 테스트: `tests/test_executor_guards.py`.

## 2. 돈을 옮기지 않는다

- executor는 계좌 간 이체·입출금을 **절대 하지 않는다**. 브로커 인터페이스(`executor/broker/base.py`)에는
  `get_balance·get_positions·place_order·cancel_order·get_order_status·list_orders`(조회) 여섯 메서드만 있다.
- 매달 넣는 돈을 어느 계좌에 넣을지는 사람이 정하고 사람이 옮긴다. executor는
  `python -m executor.scripts.add_contribution`으로 **사실을 기록만** 한다.
- 자동매매 계좌에 돈을 더 넣어도 운용 한도는 늘지 않는다. 늘어난 돈은 "한도 밖 현금"이 된다.
- 테스트: 이체·출금·입금·송금 관련 식별자나 그런 경로 문자열이 executor 코드에 없는지 정적 검사.

## 3. 자본 사다리 (`config/capital_tiers.local.yaml`, 저장소에는 `capital_tiers.example.yaml`)

실제 운용 금액은 `config/capital_tiers.local.yaml`(gitignore)에만 둔다. 저장소의 `.example.yaml`은 예시값이며, local 파일이
없으면 예시값으로 돌고 executor 화면에 "예시 설정"이 표시된다(`executor/config_files.resolve_config`).

| 단계 | 종류 | 운용 한도 | 상태 |
|---|---|---|---|
| shadow | 가상 | 설정값(예시 1,000,000원) | 확정 |
| paper | 가상(모의투자) | 설정값(예시 1,000,000원) | 확정 |
| live_1 | 실제 자금 | 설정값(예시 1,000,000원) | 금액 확정(승격 기준은 미확정) |
| live_2 | 실제 자금 | 미확정 | 미확정 → 승격 불가 |
| live_3 | 실제 자금 | 미확정 | 미확정 → 승격 불가 |

규칙:
1. **주문 계산 자본 = min(운용분 평가금액, 현재 단계 한도).** 운용분(슬리브)은 executor 원장(배정액 + 매매 손익)으로만
   움직인다. 계좌 평가금액을 직접 쓰지 않는 이유: 계좌에는 한도 밖 현금이 같이 있어서, 그걸로 min을 잡으면
   손실을 한도 밖 현금이 자동으로 메운다. 슬리브 기준이면 **손실이 나면 줄어든 금액 그대로 운용한다**.
2. 한도 밖 현금 = 계좌 평가금액 − 운용 자본. 화면·스냅샷에 따로 표시한다.
3. 한도 강제는 두 겹이다: 계획 단계에서 수량을 한도 안으로 계산하고(`planner.plan_orders`), 보내기 직전에
   "주문 후 보유 평가액 ≤ 운용 자본"과 "매수 총액 ≤ 운용 자본"을 다시 확인한다. 넘으면 **계획 전체를 거부**하고
   (`cap_rejected` 사건) 하나도 보내지 않는다.
4. 계좌는 셋이다: `shadow`·`paper`(가상 단계마다 별도 원장)와 `real`(실제 자금 단계 공용). 가상 단계에 있을 때 실제
   계좌의 운용 자본은 0이다. 이미 지나온 가상 계좌(특히 섀도)는 더 높은 단계에서도 자기 한도로 계속 돌며 비교 기준이 된다.
5. 한도가 "미확정"인 단계의 운용 자본은 0이다.

## 4. 승격은 수동, 강등은 자동

**승격**
- `python -m executor.scripts.promotion_report` — 현재 단계 성과를 `config/promotion_criteria.yaml`과 비교해서
  항목별 PASS/FAIL을 보여주고 "승격 가능/불가"만 표시한다. **반영하지 않는다.**
- `python -m executor.scripts.promote_tier` — 사람이 직접 실행하고 `"<현재>에서 <다음>로 승격합니다"`를 정확히
  입력해야 반영된다. 승격 시점에 기준을 **다시 계산**한다(과거 리포트의 PASS만 믿지 않음).
- 승격이 거부되는 경우: 기준 stage `confirmed: false` · 항목 값 "미확정" · FAIL 항목 · 다음 단계 미확정 ·
  값이 없는 지표(데이터 없음은 FAIL) · 확인 문구 불일치 · 계좌 정지 상태 · 실제 자금 단계인데 계좌 평가금액 미입력.
- 실제 자금 단계로 승격할 때 운용분 배정 = min(사람이 입력한 계좌 평가금액, 다음 단계 한도).

**강등** (단계별 `demotion`, 하나라도 넘으면 한 단계 내림, `operator='auto'`)
- 단계 진입 후 운용분 MDD > `max_mdd_pct`
- 비교 기준(스냅샷 reference_value, 없으면 실제 계좌는 같은 날 가상 계좌) 대비 누적수익률 괴리 > `max_divergence_pct`
- 주문 오류·거절 건수 > `max_order_errors`
- 잔고 불일치 건수 > `max_reconciliation_mismatches`
- 판정 시점: 주문 **전**(오늘 평가로 임시 스냅샷을 남긴 뒤)과 주문 **후**. 운용 자본 계산은 즉시 줄어든다.
- 강등 기준은 `confirmed: false`여도 적힌 값으로 작동한다(피해를 줄이는 방향이라 꺼두지 않는다).
- **강등되면 그 계좌를 정지한다**(2026-10-18 사용자 결정, 자동 정리 없음):
  - MDD·괴리(그리고 주문 오류) → `review_required`: 신규 주문 정지 + **보유 유지** + "수동 판단 필요". 정리할지는 사람이
    정한다 — `python -m executor.scripts.resume --account real --reason "정리하기로 함"`으로 해제하면 다음 실행부터 줄어든
    운용 자본에 맞춰 주문(=정리)하고, 보유를 유지하려면 해제하지 않는다.
  - 잔고 불일치 → `halt`: 정리 주문을 포함한 **모든 주문 정지**. 원장·브로커 차이를 직접 정리한 뒤 resume.
  - 주문 오류를 `review_required`로 묶은 것은 Claude의 판단이다(사용자는 MDD·괴리·불일치만 지정) — 오류 직후 자동으로
    파는 것도 같은 위험이라 보수적으로 정했다. 바꾸려면 `ladder_ops.freeze_kind_for`.
- **cap_rejected**(한도 초과로 계획 전체 거부): 1회는 강등 사유가 아니다. 연속 `cap_rejected_streak`회(config/executor.yaml,
  기본 3)에 닿을 때마다 주문 오류 1건으로 센다. 정상 실행이 한 번 끼면 연속 횟수는 0부터.
- 강등 뒤 재승격은 다시 promote_tier 절차를 거친다. 자동 재승격 경로는 없다.

## 5. 기록 (executor.db, append-only)

`tier_history`(단계 변경·사유·시점) · `sleeve_ledger`(배정·매매) · `exec_events`(주문 오류·거절·불일치·정지) ·
`promotion_results` · `config_history`(설정 파일 내용이 바뀔 때마다 전체 내용) · `contributions` · `savings_valuations`.
이 테이블들은 INSERT만 한다(UPDATE·DELETE 코드가 없는지 테스트). `orders`는 상태 갱신이 있고, (전략, 종목,
결정일, 계좌) 키로 같은 주문을 두 번 보내지 않는다. `snapshots`는 (날짜, 계좌)로 멱등.

## 6. 섀도 실행·킬 스위치·캘린더·신선도·KisBroker (2026-10-18)

**섀도 실행기** (`executor/shadow.py`, `python -m executor.scripts.run_shadow`)
- 오늘(KST) 장 마감(15:40) 후 한 번. baseline 위험 점수 모델(결정일 정렬 + 그 날짜 이전 weight_history + 분리 방식 점수 →
  단계 → `config/executor.yaml` shadow.stage_weights)로 목표 비중을 정하고, "보냈을 주문"을 `orders_shadow`에 기록한다.
  실제 브로커 호출은 없다 — 섀도 원장 안에서 체결 처리(ShadowBroker)해서 섀도 계좌의 운용분·스냅샷이 쌓인다. 기본 체결 시점은
  `next_open`(2026-10-19): t일 저녁 실행이 (1) 전날 결정한 주문을 오늘 시가로 체결하고 전날 종가 대비 시가 갭을 `shadow_gaps`에
  남긴 뒤 (2) 오늘 종가로 다음 결정을 내려 `pending_decisions`에 둔다. `config/executor.yaml`의 `fill: same_close`로 예전 방식
  (그날 종가 결정·체결)을 쓸 수 있다.
- 비교 기준(reference_value) = 같은 규칙을 섀도 시작일부터 `analysis/what_if.simulate_shares`(정수 주식)로 돌린 백테스트
  자산. 목표 수량은 엔진과 백테스트가 같은 함수 `analysis/position_policy.target_quantity`로 정한다(2026-10-19, 기본: 목표
  비중이 바뀔 때만 다시 계산 · 정수 주식 · 버퍼 0 · 보유가 운용 자본을 넘으면 다시 계산). 그래서 섀도가 정상이면 매일
  운용분 = 비교 기준이고(테스트: 정확히 일치), 괴리는 실행 쪽(부분 체결·거부·지연·정지)에서만 생긴다. 직전에 체결까지 끝낸
  목표는 `target_history`(append-only)에 남긴다 — 계획한 주문이 전부 체결됐을 때(또는 주문이 필요 없었을 때)만 기록한다.
  체결 시점은 실제로 가능한 "t일 종가 결정 → t+1일 시가 체결"로 정했다(docs/reference_gap_analysis.md N-9). 백테스트도 같은
  가정을 쓴다. 모의투자에서 장 시작 전 시장가 주문이 시가 단일가에 들어가는지는 확인 필요.
- 실행하지 않는 경우(사유만 `shadow_runs`에): 킬 스위치("킬 스위치로 차단됨"), 캘린더 범위 밖, 주말·휴장일(대체공휴일 포함),
  오늘이 아닌 날짜, 장 마감 전, 데이터 신선도 실패, 섀도 계좌 정지.
- **앞으로만 돈다.** 과거 날짜 실행 기능은 없다(지난 날짜는 what_if 백테스트가 측정).

**위험 점수 방식**(2026-10-19): 섀도는 `shadow.score_method`(기본 `group`)로 단계를 정한다 — 신호 그룹 균등 가중치 +
기준 기간(2005~2018, 한국 거래일) 백분위 기준점. 기준 기간 데이터가 없으면 `skipped_model`로 건너뛴다(기준점을 지어내지 않음).
`weighted`는 예전 적중률 가중치 방식.

**휴장일 캘린더** (`config/krx_holidays.yaml`, `executor/krx_calendar.py`)
- 2026년 평일 휴장일(대체공휴일 포함). 지나간 날은 baseline.db 세 종목(000660·005930·KOSPI)에 시세가 없는 평일과 정확히
  일치하는 것으로 확인(`verified: data`, 7/17 제헌절 포함). 12/25·12/31은 `unverified`. **범위 밖(2027~)은 실행하지 않는다**
  — 연초에 다음 해 목록을 추가해야 한다.

**다음 해 휴장일 경고**(2026-10-19): 12/1 이후인데 캘린더가 다음 해를 덮지 않으면(`krx_calendar.next_year_warning`) run_shadow가
결과와 무관하게 `calendar_warning` 사건을 남기고 CLI가 출력하며, executor 화면(`executor.web`) 맨 위에 경고 카드가 뜬다.

**데이터 신선도** — 결정일 종가가 있어야 하고, 한국 장 신호는 결정일 값, 해외 세션 신호는 결정일 이전 값의 원래 날짜가
`freshness.max_foreign_signal_age_days`(기본 4) 이내, weight_history 기록이 있어야 한다. 값이 원래 없는 신호(수동 입력 미입력)는
대상에서 뺀다.

**킬 스위치** (`executor/kill_switch.py`) — 저장소 루트 `KILL_SWITCH` 파일(`touch KILL_SWITCH`) **또는** DB 값
(`python -m executor.scripts.kill_switch on --reason ...`) 중 하나만 켜져도 실행 시작과 **주문 하나하나 직전**에 멈춘다.

**KisBroker** (`executor/broker/kis.py`) — 모의투자 전용 골격. 설정은 실전과 분리: `.env.paper` 또는 환경변수
`KIS_PAPER_APP_KEY`·`KIS_PAPER_APP_SECRET`·`KIS_PAPER_ACCOUNT`(예: `12345678-01`)·`KIS_PAPER_BASE_URL`(생략 시 모의투자 URL).
시작 단계에서 거부: 모의투자 URL이 아님 / 모의 키가 실전 키(`KIS_APP_KEY`·`KIS_APP_SECRET`)와 같음 / 누락·형식 오류.
tr_id·경로·필드는 koreainvestment/open-trading-api(commit 2077773) 샘플 기준: 매수 VTTC0012U · 매도 VTTC0011U · 취소 VTTC0013U ·
잔고 VTTC8434R · 체결조회 VTTC0081R. 실제 호출 테스트는 `KIS_PAPER_TEST=1`일 때만(잔고 조회만).

**재시작 복구** (`executor/recovery.py`, 2026-10-19) — `run_once`·`execute_pending`이 킬 스위치·정지 확인 다음, 잔고 대사
전에 한 번 돈다. 주문 행은 전송 직전 `sending` → 접수 `submitted` → 결과 확정 뒤에만 끝난 상태로 바뀌므로, `sending`·`submitted`로
남은 행이 "결과를 모르는 주문"이다. 브로커의 `list_orders`(KIS: 일별 주문체결 조회 VTTC0081R, 조회 전용)로 확인해서
- **자동**: 브로커가 끝난 주문(전량 체결·잔량 취소 확인·거부)으로 보고하면 원장에 없는 체결분만 기록하고 상태를 확정한다.
  주문번호 없이 죽은 `sending` 행은 같은 종목·방향·수량 이하인, 아직 짝이 없는 브로커 주문이 정확히 하나이고 끝난 주문일
  때만 짝짓는다(뒤이은 잔고 대사가 다시 검증). 섀도는 원장이 곧 브로커라 흔적이 없으면 `not_sent`.
- **정지(halt) + 수동 판단**: 브로커에 살아 있는 잔량이 있음(확인된 체결분은 먼저 원장에 반영) / 짝이 0개·2개 이상 /
  체결 수량이 원장보다 적음 / 조회 실패. 사람이 증권사에서 처리(예: 잔량 취소)한 뒤 `executor.scripts.resume`으로 풀면
  다음 실행의 복구가 끝난 주문으로 확정한다. 복구는 주문을 보내지도 취소하지도 않는다.
- 복구한 주문이 전량 체결이 아니면 그 대기 결정은 `incomplete`로 닫는다(남은 주문은 다음 결정이 다시 계산).
- 사건: `recovery_auto` / `recovery_manual`. 테스트: `tests/test_executor_recovery.py`(전송 직후 종료 → 재시작, 부분 체결 중
  종료 → 재시작, 애매한 경우 정지).

**보안** — 키·시크릿·토큰·계좌번호는 `executor/redact.py`로 모든 예외 메시지에서 지우고, repr에는 설정 여부만. 토큰은 메모리에만.
주문 엔드포인트 문자열은 `executor/broker/`에만 있고 실전 주문 tr_id는 저장소 어디에도 없다(정적 검사).

**운영 제한** (`executor/planner.py`, 2026-10-19, 값은 `config/executor.yaml` — 전부 미확정 시작 기본값)
- 종목당 최대 비중 `concentration.max_single_weight`(기본 30%): 개별 종목 목표 비중을 자르고 나머지는 현금. 지수 ETF
  (`exempt`: 069500·SPY·QQQ)는 예외. 엔진은 목표 비중을 맨 처음 자르고(체결 완료 목표도 자른 값), 섀도는 단계 비중에 먼저 적용해서
  비교 기준 백테스트도 같은 값을 쓴다. **000660을 대상으로 하면 단계 비중 100/66/33%가 전부 30%가 된다**(2026-10-19부터 섀도 대상은 예외 목록의 069500이라 해당 없음; 단계 구분이 사실상
  보유/미보유 둘로 줄어듦). 1주가 약 127만원이면 운용 자본이 약 423만원보다 작을 때 30%로는 1주도 못 산다.
- 일일 제한 `daily_limits`: 손실률(운용분이 직전 스냅샷 대비 3% 이상 하락 → 그날 매수 보류) · 주문 횟수(같은 결정일 4건 초과 →
  계획 전체 거부, 상태 `daily_limit`) · 쿨다운(강등·정지 사건일로부터 5일 안 → 매수 보류, 해제 뒤에도). 매도는 손실·쿨다운
  제한에서도 진행한다. 매수를 보류한 결정은 체결 완료 목표로 남기지 않아 다음 결정이 다시 계산한다. 판정 입력은 전부 executor.db
  (스냅샷·주문·사건)라 재시작해도 유지된다. 사건: `daily_limit`·`concentration_cap`.

**아직 없는 것**: REDUCING 상태, vectorbt 교차 검증, KisBroker를 섀도 실행기처럼 매일 돌리는 paper 실행기,
실제 자금 브로커(범위 밖, 0절).

## 7. CLAUDE.md 원칙 문구 변경 **안** (적용하지 않음 — 사용자 확정 필요)

- 원칙 2: "baseline(analysis·db·collectors·web·scripts·mcp_server·agents)은 매매 기능을 구현하지 않는다. 주문 코드는
  `executor/broker/`에만 두고 EXECUTOR.md 규칙을 따른다."
- 보안 원칙 6: "baseline 코드는 KIS 주문 API를 호출하지 않는다. executor의 주문은 모의투자 URL·모의투자 키로만
  허용한다. 실전 주문 경로는 별도 확정 전까지 구현하지 않는다."

## 8. 매일 운영 (섀도)

수동(장 마감 후, 저장소 루트에서):
```
python collectors/collect_daily.py        # 시세 수집 (yfinance)
python scripts/compute_signals.py         # 신호값 갱신
python -m executor.scripts.run_shadow     # 섀도 실행 — 휴장일·데이터 오래됨이면 사유만 기록하고 끝
python -m executor.scripts.run_shadow --status
```
Windows 작업 스케줄러 예시(**예시일 뿐, 등록하지 않았다**). `C:\baseline\run_shadow.bat`:
```
@echo off
cd /d C:\baseline
call .venv\Scripts\activate.bat
python collectors\collect_daily.py >> logs\shadow.log 2>&1
python scripts\compute_signals.py >> logs\shadow.log 2>&1
python -m executor.scripts.run_shadow >> logs\shadow.log 2>&1
```
등록(관리자 권한 불필요, 평일 16:30):
```
schtasks /Create /TN "baseline-shadow" /TR "C:\baseline\run_shadow.bat" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:30
```
휴장일에 돌아도 섀도 실행기가 스스로 건너뛰고 사유를 남기므로 스케줄에서 휴장일을 따로 뺄 필요는 없다. PC가 꺼져 있던 날은
다음 날 "오늘이 아닌 날짜"라 소급 실행되지 않는다(의도된 동작 — 섀도는 그날 알 수 있던 데이터로 그날 계산한 기록).
