# 참고 오픈소스 대비 차이 분석 — baseline / executor

작성: 2026-10-18 · 대상 독자: 이 저장소 개발자(본인)와 다음 세션 · **분석·문서 전용 — 이 작업에서 코드는 바꾸지 않았다.**

참고 저장소는 읽기만 했고, 실행·빌드하지 않았다. 코드를 복사하지 않았고 **설계만** 참고했다. 이 문서에
적힌 우리 코드의 동작은 전부 파일을 직접 읽어서 확인했다. 확인하지 못한 것은 "확인 필요"라고 적었다(11절).

> **보류 대상의 현재 상태**: 요청에서 보류하라고 한 섀도 실행기(`executor/shadow.py`)와 KisBroker
> (`executor/broker/kis.py`)는 이전 세션(2026-10-18 5차)에 **이미 작업 트리에 만들어져 있다(아직 커밋 안 됨)**.
> 그래서 이 분석의 대상에 넣었고, 이 문서의 결론이 나오기 전까지는 **기능을 더 붙이지 말 것**을 권한다.

---

## 0. 요약

| | 결론 |
|---|---|
| 가장 큰 위험 | **백테스트(`analysis/what_if.py`)와 실행(`executor/planner.py`·`engine.py`)이 비중을 포지션으로 바꾸는 코드를 따로 갖고 있다.** 비중 드리프트 처리, 정수 주식, 비용, 설정값이 서로 다르다. 그런데 섀도 계좌의 "괴리"(이 차이를 포함한 값)가 **자동 강등 기준**으로 쓰인다(7절). |
| 두 번째 위험 | 체결 가정 "t일 종가로 결정하고 t일 종가에 체결"은 KisBroker가 보내는 **시장가(ORD_DVSN `01`) 주문으로는 실현할 수 없다.** 섀도는 장 마감 뒤(15:40 KST)에 돌기 때문이다(3절 N-9). |
| 가중치 | 적중률 기반 가중치 최적화는 pysystemtrade의 원칙과 정반대 방향이다. 실제로도 **유의한 신호 0/14개**, v3 백필의 근거 있는 스냅샷 **8/286개**라서 결과는 거의 "균등 초기값"이다. 장치는 크고 정보는 거의 없다. **고정된 그룹 균등 가중치**를 권한다(6절). |
| 방향 | **선택지 A(지금 구조 유지 + 개선)**를 권하고, C의 일부(vectorbt를 **교차검증 기준(오라클)**으로만 사용)를 함께 쓴다. B(NautilusTrader로 교체)는 KIS 어댑터를 직접 만들어야 하고, Nautilus v2가 아직 RC 단계라 지금은 맞지 않는다(8절). |

**가장 먼저 고칠 것 3가지**(상세는 10절):
1. 비중→수량 변환을 순수 함수 하나로 합치고, what_if와 planner가 둘 다 그 함수를 쓰게 한다(버퍼·정수 주식·비용·단계 비중을 한 곳에서 관리). 그다음 백테스트 대 섀도 동치 테스트를 "0.5%p 미만"이 아니라 **수량 경로가 정확히 같은지**로 바꾼다.
2. 체결 시점을 실제로 가능한 방식 하나로 정하고(장후 시간외 종가 `06` 또는 t+1일 시가), **백테스트도 같은 가정**을 쓰게 한다. 그 전에는 KisBroker로 모의 주문을 보내지 않는다.
3. 가중치를 "적중률 최적화"에서 "고정된 그룹 균등 + 사람이 명시적으로 바꿀 때만 변경"으로 바꾼다. 위험 단계 임계값(60/80)은 가중치 합 단위라서 같이 다시 정해야 한다.

---

## 1. 참고 저장소

`../refs/`에 shallow clone(`--depth 1`). 날짜는 clone한 HEAD 커밋의 커밋 날짜다.

| 저장소 | 라이선스 | 마지막 업데이트(HEAD) | 커밋 | 이 분석에서 본 부분 |
|---|---|---|---|---|
| pst-group/pysystemtrade | GPL-3.0 | 2026-10-16 | `164328b` | `systems/forecast_scale_cap.py`·`forecast_combine.py`·`positionsizing.py`·`buffering.py`, `sysquant/estimators/forecast_scalar.py`, `sysquant/optimisation/`, `sysdata/config/defaults.yaml`, `docs/backtesting.md` |
| nautechsystems/nautilus_trader | LGPL-3.0 | 2026-10-18 | `6875261` | `docs/concepts/live.md`·`execution/reconciliation.md`·`execution/policies.md`·`orders/index.md`, `docs/integrations/interactive_brokers.md`, `crates/adapters/interactive_brokers/`, `MIGRATION_V2.md` |
| QuantConnect/Lean | Apache-2.0 | 2026-10-16 | `d4cac37` | `Algorithm.Framework/`(Alphas·Portfolio·Risk·Execution), `Algorithm/{Portfolio,Risk,Execution}`, `Common/Algorithm/Framework/Alphas/Insight.cs`, `Common/Brokerages/InteractiveBrokersBrokerageModel.cs` |
| koreainvestment/open-trading-api | **LICENSE 파일 없음**(명시 없음) | 2026-10-14 | `2077773` | `examples_llm/domestic_stock/*`, `examples_llm/overseas_stock/*`, `legacy/postman/모의계좌_*.json`, `backtester/README.md` |

- **라이선스 의미**: pysystemtrade(GPL-3)의 코드를 가져오면 배포 시 GPL 의무가 생긴다. 그래서 설계만 참고하고 코드는 한 줄도 가져오지 않는다. Nautilus(LGPL-3)는 라이브러리로 쓰는 것은 괜찮지만, 고쳐서 배포하면 그 부분을 공개할 의무가 생긴다. Lean(Apache-2.0)은 허용 범위가 넓다. 한투 저장소에는 라이선스가 명시돼 있지 않아서 기본 저작권이 적용된다고 보고, **API 명세(경로·tr_id·필드명)라는 사실 정보만** 참고한다.
- `../refs/freqtrade`(GPL-3.0, 2026-10-18)도 있지만 이번 요청 대상이 아니어서 분석하지 않았다.
- vectorbt는 clone하지 않았다. 라이선스는 "Apache-2.0 + Commons Clause"로 알고 있지만 **확인 필요**하다.

---

## 2. pysystemtrade — 전략·비중 결정

비교 대상: `analysis/signals.py`, `analysis/weighting.py`, `analysis/risk_score.py`, `analysis/what_if.py`(단계→비중)

| # | 항목 | 우리 구현 | 참고 구현 | 차이 | 위험도 |
|---|---|---|---|---|---|
| P-1 | 신호 표현 | 신호마다 `is_on` 0/1(`value` 컬럼은 있지만 점수 계산에는 `is_on`만 쓴다). 미국 10년물이 4.51%여도 5.5%여도 똑같이 "켜짐" | 연속 forecast. 평균 \|forecast\| = 10이 되도록 스케일러로 맞춘다(`average_absolute_forecast: 10`, 스케일러는 고정값이거나 전체 기간 확장 창으로 추정, `min_periods: 500`) | 강도 정보가 사라진다. 임계값 근처에서 켜졌다 꺼지는 잡음이 그대로 비중 변경으로 이어진다 | 중간 |
| P-2 | 상한(cap) | **가중치**에 상한: 신호 30%, 전문가 합 40%. water-filling으로 재배분 | **forecast 값**에 상한(`forecast_cap: 20`, 평균의 2배). 결합 후 분산 배수(FDM)에도 상한(`dm_max: 2.5`) | 우리 상한은 "신호 하나에 너무 많이 의존하지 말자"는 목적이고, 참고 구현의 상한은 "극단값 하나가 포지션을 키우지 못하게"다. 극단값 보호는 우리 쪽에 없다. 다만 0/1 신호라 극단값 자체가 없어서 지금은 문제가 안 된다 | 낮음 |
| P-3 | 결합 → 포지션 | 켜진 risk 가중치 합 → 점수 (합+100)/2 → 임계값 60/80 → 단계 → 비중 100/66/33/0%(계단) | 가중합 × FDM → 연속 포지션 | 계단형이라 작은 입력 변화에 비중이 34%p 단위로 뛴다(2026-10-18 what_if: 내내 66%였다가 한 번에 바뀜) | 중간 |
| P-4 | 가중치 결정 | 적중률의 기준률 대비 edge × 표본 축소. CI 하한 ≤ 기준률이면 0. 매주 재계산(walk-forward), 알고리즘 버전 3개(v1~v3) | **기본은 추정하지 않음**(`use_forecast_weight_estimates: False` → 설정 파일의 고정값, 없으면 균등). 추정할 때는 handcrafting을 권장하고, shrinkage는 SR을 90%까지 균등 쪽으로 당긴다(`shrinkage_SR: 0.9`). 가중치는 EWMA 125영업일로 평활하고, 비용을 반영한 SR로 페널티를 준다 | 우리는 "데이터가 말하는 만큼 준다"는 방향이고, 참고 구현은 "데이터는 거의 아무것도 말해주지 못한다고 가정한다"는 방향이다. 6절에서 자세히 | **높음** |
| P-5 | 상관·중복 처리 | 사람이 정한 그룹 1개(US10Y)에 대한 **경고만**. 전문가(사람) 단위 40% 상한 | 상관 기반 handcrafting(비슷한 규칙끼리 묶어 그룹별로 나눔) + FDM | 우리 "전문가" 묶음은 정보의 출처가 아니라 **말한 사람** 기준이다. 같은 지표(^TNX)에서 나온 YJH_US10Y·KSJ_US10Y_HIGH는 서로 다른 전문가라 상한이 적용되지 않는다. KHJ_COMBO는 하위 신호와 정보가 겹친다(CLAUDE.md 2026-10-18 기록) | 중간 |
| P-6 | 균등 초기값 | "모든 신호 동일"을 의도했다. 그런데 **전문가 40% 상한 때문에 실제로는 같지 않다**: KHJ 6개는 각 6.67%, 나머지 8개는 각 7.5%(실DB `get_current_weights()`로 확인) | 균등(equal_weights)은 정말로 균등하다 | 의도와 결과가 다르다. 신호가 많은 전문가는 신호 하나당 가중치가 작아진다 | 낮음 |
| P-7 | 비용 | 가중치를 정할 때 회전율·비용을 보지 않는다. 비용은 what_if 시뮬레이션에만 들어간다 | 가중치에 비용 반영 SR 페널티(`cost_multiplier`), 회전율 상한("speed limit") | 우리 모델은 거래가 드물어서 영향이 작다 | 낮음 |
| P-8 | 포지션 크기 | 단계별 고정 비중. 종목 변동성과 관계없다 | 변동성 목표(`percentage_vol_target: 16`): 포지션 = 자본 × 목표변동성 / 종목변동성 × forecast/10 | 000660처럼 변동성이 크고 시기마다 크게 달라지는 종목에서는, 같은 "100% 보유"라도 날마다 떠안는 위험이 몇 배씩 다르다 | 중간 |
| P-9 | 버퍼(포지션 관성) | **planner에는 없다** — 목표 수량을 매일 다시 계산해서 드리프트를 매일 맞춘다. what_if는 목표가 바뀔 때만 거래한다(7절) | `buffer_size: 0.10` — 최적 포지션 ±10% 범위 안이면 거래하지 않는다 | 백테스트와 실행이 서로 다른 규칙을 쓰게 된 원인이다 | **높음** |
| P-10 | 분산 | 종목 1개(000660). 069500은 비교 기준일 뿐 | 여러 종목 + 종목 가중치 + 종목 분산 배수(IDM) | 집중 위험이 구조에 박혀 있다. 학습 프로젝트라 의도한 범위일 수 있다 | 중간 |
| P-11 | 롱 전용 | 롱 전용 | `long_only_instruments` 목록으로 지원 | 같다 | — |

---

## 3. NautilusTrader — 실행 구조

비교 대상: `analysis/what_if.py`, `executor/planner.py`·`engine.py`·`shadow.py`·`broker/`

| # | 항목 | 우리 구현 | 참고 구현 | 차이 | 위험도 |
|---|---|---|---|---|---|
| N-1 | 백테스트와 실전이 같은 코드를 쓰는가 | 아니다. 같은 것은 **점수·단계 계산까지**(`what_if.score_and_stage`). 비중→포지션은 백테스트(`what_if.simulate`, 비율 단위, 드리프트 무시)와 실행(`planner.plan_orders`, 정수 주식, 매일 목표 수량)이 따로 갖고 있다 | 같은 `Strategy` 클래스를 쓰고, 거래소만 바뀐다(가상 거래소 ↔ 어댑터). 공식 문서(live.md): "A live node shares strategy and execution-algorithm code with a backtest" | 7절 | **높음** |
| N-2 | 주문 상태 | `planned → submitted → filled/partial/rejected/error`, 브로커는 `cancelled`도 돌려준다(그런데 `executor/schema.sql` 주석에는 cancelled가 빠져 있다) | 상태 기계: INITIALIZED·SUBMITTED·ACCEPTED·PARTIALLY_FILLED·PENDING_CANCEL·PENDING_UPDATE·FILLED·CANCELED·EXPIRED·DENIED 등. 상태 전이마다 이벤트를 남긴다(event sourcing) | 우리는 전이 이력이 아니라 마지막 상태만 덮어쓴다(`update_order`). 접수(ACCEPTED)와 진행 중 취소(PENDING_CANCEL) 상태가 없다 | 중간 |
| N-3 | 결과를 알 수 없는 주문(타임아웃) | `place_order`가 `BrokerError`를 던지면 `status='error'`로 기록 → 주문 후 대사에서 수량이 맞지 않으면 halt. **fail-closed라서 안전**하지만 주문 조회로 원인을 풀지는 않는다 | "unknown outcome" 상태를 따로 둔다. 진행 중으로 유지하다가 스트림·조회·대사로 결론을 낸다. HTTP 오류만으로는 거래소가 실제로 무엇을 했는지 확정하지 않는다(policies.md) | 안전성은 같다(멈춘다). 다만 멈춘 뒤 사람이 직접 확인해야 하는 일이 많다 | 중간 |
| N-4 | 부분 체결·폴링 | 상태 조회를 **기다림 없이 연속 3회**(`MAX_POLLS=3`, sleep 없음) 하고, 남은 수량은 즉시 취소한다 | 이벤트 기반 체결 수신 | KIS 체결조회(VTTC0081R)에 반영이 늦으면, 곧 체결될 시장가 주문을 바로 취소 요청하게 된다. 결과적으로 체결 수량이 운에 좌우된다 | 중간 |
| N-5 | 재시작 시 복구 | 프로세스 메모리에 상태를 두지 않는다. 매 실행 시작 때 브로커 잔고와 내부 원장을 대조하고, 다르면 halt. 같은 날 다시 실행하면 같은 `order_key`는 건너뛴다. **크래시로 `planned/submitted`에 남은 주문은 다시 조회하지 않는다** | 시작할 때 대사: 거래소 보고를 기준(권위)으로 보고 빠진 주문·체결 이벤트를 생성한다. "보고가 없다고 포지션 0인 것은 아니다". 해결이 안 되면 전략을 시작하지 않는다(fail closed) | 기본 방향(브로커가 권위, 안 맞으면 멈춤)은 같다. 우리 쪽에 없는 것은 "남은 주문 조회로 원인 풀기"와 "체결 내역 기반 재구성" | 중간 |
| N-6 | 전송 전에 기록 | `insert_order(status='planned')` → `place_order` 순서 | "Persistence before transport" 정책 | **이미 갖췄다** | — |
| N-7 | 브로커 어댑터 구조 | `Broker` ABC 메서드 5개(잔고·포지션·주문·취소·상태), 동기 호출 | `InstrumentProvider`(종목 규칙: 호가 단위·로트·거래 시간) + `DataClient` + `ExecutionClient`. 대사용 보고서 생성(주문 상태·체결·포지션 보고) 메서드가 따로 있다 | 우리에게 없는 것: **체결 내역 조회**, **미체결 주문 목록**, **종목 규칙**(호가 단위·가격제한·주문 유형별 접수 시간) | 중간 |
| N-8 | IB 어댑터 | — | TWS/IB Gateway 소켓 API 경유(모의 포트 7497/4002). Docker용 Gateway 관리 기능이 있다. 주문 식별은 IB permanent ID, 계좌가 정해지지 않은 주문은 리스크 엔진이 거부한다. 기본은 읽기 전용 API(`read_only_api`) | IB로 갈 경우 참고할 부분은 8절 B | — |
| N-9 | 거래 시각·주문 유형 | 섀도는 15:40 KST 이후 t일 종가로 결정하고 **t일 종가에 체결**된 것으로 처리한다. KisBroker는 `ORD_DVSN "01"`(시장가)을 보낸다 | 거래소 세션과 주문 유형 규칙은 어댑터·종목 규칙이 맡는다 | 정규장(15:30 마감)이 끝난 뒤 그날 종가에 체결되려면 **장후 시간외(`06`)**만 가능하다(한투 모의계좌 Postman 샘플의 ORD_DVSN 설명: 05 장전 시간외, 06 장후 시간외). 시장가 `01`이 장 마감 후 거부되는지 다음 날로 넘어가는지는 **확인 필요**하지만, 어느 쪽이든 백테스트 가정과 다르다 | **높음** |
| N-10 | 사전 리스크 검사 | `check_plan_within_cap` + 주문마다 킬 스위치 확인 + 단계 한도 | RiskEngine이 주문을 DENIED 처리(한도·잘못된 주문) | 단순하지만 **갖췄다** | — |

---

## 4. Lean — 모듈 분리 구조

Lean Algorithm Framework의 흐름: **Universe Selection → Alpha(Insight) → Portfolio Construction(PortfolioTarget) → Risk Management(목표를 조정) → Execution**. 각 단계는 인터페이스(`IAlphaModel`, `IPortfolioConstructionModel`, `IRiskManagementModel`, `IExecutionModel`)로 분리돼 있고 바꿔 끼울 수 있다.

| Lean 단계 | 우리 대응 | 차이 | 위험도 |
|---|---|---|---|
| Data | `collectors/*` → `baseline.db`, `analysis/signal_alignment.py`(해외 세션 정렬) | 해외 신호를 결정일 기준으로 정렬하는 부분은 잘 돼 있다 | — |
| Universe Selection | 000660 고정 | 해당 없음 | 낮음 |
| Alpha (`Insight`: direction·period·magnitude·confidence·weight) | `analysis/signals.py` + `scripts/compute_signals.py` → `signal_values`(is_on, value) | 우리 신호에는 **유효 기간(period)이 없다.** 평가는 5/20/63일로 하지만 신호 자체는 "언제까지 유효한지"를 갖고 있지 않다. 강도도 없다(P-1) | 낮음~중간 |
| Portfolio Construction (→ `PortfolioTarget`) | `risk_score.compute_risk_score`·`determine_status` → `what_if.stage_from_status` → 단계 비중. **비중은 두 곳에 있다**: `what_if.DEFAULT_STAGE_WEIGHTS`와 `config/executor.yaml` `shadow.stage_weights`(주석에 "같은 값"이라고 적혀 있을 뿐 강제하지 않는다) | 대시보드 상태 문구(`determine_status`의 한국어 문자열)를 문자열 비교로 단계 숫자로 바꾼다(`"1/3" in sell_stage`). 화면용 문구가 포트폴리오 구성의 인터페이스 역할을 하고 있다 | 중간 |
| Risk Management (목표를 줄이거나 0으로) | 흩어져 있다: 쏠림 경고(표시만), 점수 상한 경고(표시만), 단계 강등·정지(executor), 킬 스위치, 한도 검사(planner) | Lean은 목표가 나온 **뒤**, 실행 **전**에 한 층에서 처리한다. 우리는 "모델 리스크"(점수)와 "운영 리스크"(강등)가 섞여 있다. 운영 리스크 쪽은 오히려 Lean 기본 모델보다 촘촘하다 | 중간 |
| Execution (+ BrokerageModel: 수수료·로트·주문 유형) | `engine.run_once` + `planner` + `broker/*` | **BrokerageModel에 해당하는 것이 없다**: 호가 단위, 가격제한(`analysis/price_limits.py`는 분석용이고 실행에는 연결 안 됨), 주문 유형별 접수 시간 | 중간 |
| 결과 분석 | `what_if.summarize`·분포·백분위, `executor/criteria.py` 승격 기준 | 갖췄다 | — |

참고: Lean에는 `Market.KRX` 상수가 있지만, IB 브로커리지 모델의 기본 주식 시장은 `Market.USA`다(`InteractiveBrokersBrokerageModel.cs`). 한투 공식 저장소의 `backtester/`는 **Lean 엔진을 Docker로 돌려서** 전략을 검증하는 구조다(README). 우리 보안 원칙 4번(Docker 빌드 금지)과 충돌하므로 그대로 쓸 수 없다.

---

## 5. 갖춘 것 / 빠진 것 / 과하게 만든 것

### 이미 갖춘 것 (참고 프로젝트 수준이거나 그 이상)
- 미래참조 방지: 해외 세션 신호의 결정일 정렬, as-of 가중치(`weights_for_dates`), 백필의 as-of 절단 테스트.
- 전송 전에 기록, 멱등 주문 키, append-only 원장, 시작 시 브로커 기준 대사와 불일치 시 정지(fail-closed).
- 킬 스위치(파일 + DB, 주문마다 확인), 휴장일 캘린더(실제 시세 공백으로 검증), 데이터 신선도 검사.
- 비밀값 마스킹, 실전/모의 설정 분리, 주문 엔드포인트·실전 tr_id 정적 검사.
- 표본 수·최대손실을 같이 보여주는 평가(원칙 3·4). 다중비교 보정(BH).

### 빠진 것
| 빠진 것 | 참고 | 위험도 |
|---|---|---|
| 백테스트와 실행이 공유하는 비중→수량 함수(버퍼 포함) | Nautilus 같은 코드 · pst 버퍼 | 높음 |
| 실제로 가능한 체결 시점·주문 유형 모델 | Nautilus 어댑터 · Lean BrokerageModel | 높음 |
| 변동성 기반 크기 조절 | pst `percentage_vol_target` | 중간 |
| 연속 신호 강도 | pst forecast, Lean Insight magnitude | 중간 |
| 주문 상태 전이 이벤트, unknown outcome, 체결 내역 조회 | Nautilus | 중간 |
| 종목 규칙(호가 단위·가격제한·접수 시간)을 실행에 적용 | Nautilus InstrumentProvider · Lean SymbolProperties/BrokerageModel | 중간 |
| 독립 엔진으로 하는 손익 계산 교차검증 | (EXECUTOR.md 6절의 남은 일) | 중간 |

### 과하게 만든 것 (지금 가진 증거에 비해)
| 항목 | 이유 |
|---|---|
| 가중치 장치 전체: 2단계 water-filling 상한, 알고리즘 v1~v3, weight_history 버전 관리, 백필 4004행 × 2벌 | 유의한 신호가 0/14개라서 결과는 거의 균등이다. 복잡도가 담는 정보는 "균등 + 잡음"이다(6절) |
| 전문가 단위 40% 상한 | 정보 출처가 아니라 사람 기준이라 중복 문제를 풀지 못하고, 균등 초기값만 비틀었다(P-6) |
| executor 자본 사다리 4단계·승격/강등·적립 기록 | 근거 있는 전략이 아직 없는 상태에서 운영 장치가 먼저 커졌다. **안전장치 자체는 싸고 옳지만**, 단계·승격 기준을 더 다듬는 것은 근거가 생긴 다음으로 미뤄도 된다 |
| 섀도 → KisBroker 순서 | 섀도 기록이 한 달도 쌓이기 전에 브로커 구현이 먼저 들어왔다. 게다가 섀도의 비교 기준 자체가 7절 문제를 안고 있다 |

과하지 **않은** 것: 킬 스위치, 대사, 멱등성, 비밀값 처리, 정적 검사. 이 부분은 줄이지 말 것.

---

## 6. 결론 ① — `weighting.py`의 적중률 기반 가중치 최적화

### 6.1 pysystemtrade 원칙에 비춘 평가

pysystemtrade(그리고 그 바탕인 Carver의 접근)의 원칙은 "**가중치를 데이터로 맞추지 마라. 맞춰야 한다면 균등 쪽으로 강하게 당겨라**"다. 근거는 기본 설정 자체에 있다: 가중치 추정이 꺼져 있고(`use_forecast_weight_estimates: False`), 추정할 때도 handcrafting을 권장하며, shrinkage는 SR 추정치를 90%까지 균등 쪽으로 당기고, 결과는 125영업일 EWMA로 평활한다. 수십 년 데이터로도 규칙 사이의 Sharpe 차이를 통계적으로 구별하기 어렵다는 판단이다.

우리 구현을 이 원칙에 비춰 보면:

| 관점 | 우리 구현 | 평가 |
|---|---|---|
| 무엇을 최적화하나 | 방향 적중률(risk: 이후 수익률 < 0) | **크기를 무시한다.** 55%는 조금씩 맞고 45%는 크게 틀리는 신호도 "edge 양수"가 된다. pst는 비용을 뺀 손익(SR)을 본다 |
| 선택 + 배분 | CI 하한이 기준률을 넘으면 edge에 비례해 배분 | 선택한 다음 배분하는 방식이라 **승자의 저주**에 노출된다. 14개 신호 × 3개 기간 중 기준을 넘는 것은 운이 좋았던 것일 가능성이 크다. BH q값을 계산하지만 가중치 조건에는 쓰지 않는다 |
| 불연속성 | CI 하한이 기준률을 한 번 넘는 순간 가중치가 0에서 최대 30%로 뛴다 | pst는 shrinkage와 EWMA로 연속적이다. 우리 쪽은 재계산할 때마다 위험 점수가 계단식으로 바뀔 수 있다 |
| 실제 결과 | 2026-10-06 v1에서는 3개 신호가 각 30%를 받아 점수 80("분할 매도 2/3")이 나왔다 → 통계 검증 뒤 유의한 신호 0개 → 57.5점. v3 백필은 286개 스냅샷 중 근거 있는 것이 8개 | **과최적화가 실제로 일어났다가 잡힌 사례**다. 지금 장치가 할 수 있는 최선의 결과는 "균등"인데, 그 균등마저 전문가 상한 때문에 비틀려 있다(P-6) |
| 임계값과의 결합 | 60/80 = risk 가중치 합 20%p/60%p | 가중치가 바뀔 때마다 **단계 판정 빈도가 소리 없이 바뀐다.** 임계값을 고정해도 의미는 고정되지 않는다 |

**결론**: 적중률 기반 최적화는 pst 원칙과 반대 방향이고, 우리 데이터에서도 정보를 거의 만들어내지 못했다. 통계 검증 코드(기준률·CI·BH)는 **진단 도구로서 가치가 있다**. 문제는 그 결과를 가중치에 자동으로 넣는 연결이다.

### 6.2 대안 — 단순하고 보수적인 가중치 (제안만, 구현 안 함)

**안 1 (권장): 고정된 정보 출처 그룹 균등 가중치**
- 방향(risk / opportunity)별로 따로, 정보 출처 그룹마다 같은 몫을 주고, 그룹 안에서는 나눠 갖는다. 아래는 **예시**이고, 그룹 구분은 설계 지식이라 사용자가 확정해야 한다.

  | 방향 | 그룹(예시) | 신호 |
  |---|---|---|
  | risk | 미국 금리 | YJH_US10Y, KSJ_US10Y_HIGH |
  | risk | KHJ 복합(정보 겹침) | KHJ_COMBO, KHJ_CONCENTRATION, KHJ_IPO_WEAK, KHJ_RATE_STAGE |
  | risk | 과열 | KHJ_OVERHEAT |
  | risk | 쏠림(근사 지표) | KSJ_BREADTH |
  | risk | 박스권 상단 | YJH_BOX_RANGE_TRIM |
  | risk | 메모리 마진 | YJH_MEMORY_MARGIN |
  | opportunity | 급락 | KHJ_3DAY_SELLOFF, KSJ_PANIC_DAY |
  | opportunity | 박스권 하단 | YJH_BOX_RANGE_BUY |
  | opportunity | 밸류 하단 | YJH_PBR_FLOOR |

- 가중치는 설정 파일에 고정하고(pst의 fixed weights와 같은 방식), **사람이 명시적으로 바꿀 때만** 바뀌게 한다. 바꿀 때는 이유를 기록한다(weight_history의 live 행으로 남기면 지금 인프라를 그대로 쓸 수 있다).
- 전문가 40% 상한은 없앤다(또는 표시용으로만 남긴다). 중복은 그룹이 처리한다.
- 임계값은 "켜진 **독립 그룹 수**"로 다시 정의하는 것을 권한다. 예: 1단계 = risk 그룹 2개 이상, 2단계 = 4개 이상. 해석이 쉽고, 가중치를 바꿔도 의미가 변하지 않는다. 현재 원칙 1의 예외 조건("기계적 계산 + 근거 동반 표시")도 그대로 충족한다. **숫자 자체는 사용자가 정할 것.**
- 통계 검증(edge·CI·q값)은 대시보드와 스코어카드에 진단으로 계속 보여준다.

**안 2: 균등 쪽으로 강하게 당기는 축소**
- `w = (1-λ)·균등 + λ·데이터 기반`, λ ≤ 0.1(pst shrinkage_SR 0.9와 같은 강도). 재계산 결과는 EWMA로 평활한다.
- 지금 장치를 대부분 살릴 수 있지만, 복잡도는 거의 줄지 않는다.

**평가 지표도 같이 바꿀 것**: 신호를 고를 때 적중률 대신 **그 신호를 빼고 돌린 what_if와 넣고 돌린 what_if의 손익 차이**(비용 반영, 비겹침 구간 수 표시)를 본다. 크기와 비용을 함께 반영한 지표다.

---

## 7. 결론 ② — `what_if.py`와 `planner.py`가 따로 있는 구조

### 7.1 실제로 다른 점 (코드로 확인)

| # | 항목 | `analysis/what_if.simulate` | `executor/planner.plan_orders` + `engine` | 확인 방법 |
|---|---|---|---|---|
| D-1 | 드리프트 | 목표 비중이 **바뀔 때만** 거래한다(모듈 docstring에 명시) | 매일 `floor(w × 자본 / 가격)`으로 목표 수량을 다시 계산한다 → 가격이 움직이면 **매일 거래** | scratch 계산: 1억 원, 66% 고정, 가격 100만→105만→110만→100만→95만이면 planner는 매도 2 → 매도 1 → 매수 3 → 매수 1주를 낸다 |
| D-2 | 단위 | 비율(소수점 비중) | 정수 주식 | 000660 종가 1,272,000원(2026-10-16) 기준으로 자본 1,000만 원이면 66% → 5주(63.6%), 33% → 2주(25.4%). 낮은 단계일수록 오차가 커진다 |
| D-3 | 비용 | 수수료 + 슬리피지 + 체결일 기준 세율 스케줄(`CostModel`) | planner: 매수 수수료만 수량 계산에 반영. engine 원장: 수수료·고정 세율. 섀도 비교 기준(`shadow.backtest_reference`): 슬리피지 0, 고정 세율 | 같은 개념이 세 곳에 있다 |
| D-4 | 단계 비중 설정 | `DEFAULT_STAGE_WEIGHTS` | `config/executor.yaml` `shadow.stage_weights` | 같은 값이라는 것은 주석일 뿐이다 |
| D-5 | 체결 시점 | t일 종가 | t일 종가(섀도). KisBroker는 시장가 | N-9 |

### 7.2 위험
1. **잘못된 자동 강등, 또는 진짜 문제를 가림**: 섀도 계좌의 `reference_value`는 what_if 백테스트로 계산한 값이고, 괴리(`divergence_pct`)는 강등 기준(`max_divergence_pct`)이다. 그런데 괴리에는 체결 품질뿐 아니라 D-1~D-3의 **구현 차이**가 섞인다. 변동성이 큰 66%/33% 구간이 길어지면 실행에 아무 문제가 없어도 괴리가 쌓인다. 반대로 진짜 체결 문제를 구현 차이로 오인할 수도 있다. EXECUTOR.md 6절은 이 차이를 "정수 주식·체결 시점 차이만큼"(테스트: 5거래일 0.5%p 미만)이라고 적었지만, D-1은 거기에 포함돼 있지 않다.
2. **백테스트 숫자가 실행을 대표하지 않는다**: CLAUDE.md에 기록된 what_if 결과(예: 2026-10-06~10-16 A −20.46%, 변경 1회)는 planner가 실제로 했을 행동(매일 소량 거래, 정수 오차)과 다르다.
3. **한쪽만 고쳐지는 문제**: 비용·비중·버퍼를 바꿀 때 두(세) 곳을 같이 고쳐야 하고, 이를 강제하는 장치가 없다.

### 7.3 하나로 합치는 방법 (제안만)

의존 방향 규칙(executor → analysis만 허용)을 지키려면 공통 함수는 **analysis 쪽 순수 함수**여야 한다.

1. `analysis/`에 포지션 정책 순수 함수를 하나 둔다(이름 예시: `position_policy.target_quantity(current_qty, target_weight, price, capital, policy)`). `policy`에는 버퍼(pst처럼 목표 ±x% 안이면 그대로), 로트(정수 주식 / 0이면 비율), 매수 비용 반영, 단계 비중 표가 들어간다.
2. `what_if.simulate`가 이 함수를 매일 호출하는 루프가 되게 한다. 비율 모드는 기존 결과 재현용으로 남긴다.
3. `planner.plan_orders`는 이 함수로 목표 수량만 받고, 주문 차이 계산과 한도 검사(`check_plan_within_cap`)만 맡는다.
4. 단계 비중은 한 곳(설정 파일)에서만 읽는다. what_if CLI도 같은 파일을 읽는다.
5. **동치 테스트**: 같은 60거래일에 대해 (a) what_if 정수 모드와 (b) engine + FakeBroker(종가 즉시 체결)를 돌려서 **날짜별 수량 경로와 자산이 정확히 같은지** 확인한다. 허용 오차로 비교하지 않는다. 이후 섀도 괴리에는 체결 가격·부분 체결·지연만 남는다.
6. 더 가벼운 대안: 섀도의 비교 기준을 what_if가 아니라 **engine + ShadowBroker로 과거를 재생**해서 만든다. 다만 이 방법은 "백테스트 숫자가 실행을 대표하지 않는다"(7.2-2)는 해결하지 못하므로 1~5번을 권한다.

이 구조가 Nautilus의 원칙(같은 전략 코드에 거래소만 바꿈)을 우리 규모에 맞춘 형태다.

---

## 8. 방향 선택지 (제안만, 구현 금지)

작업량은 이 저장소의 기존 추정 단위(시간)로 적은 **대략치**다.

| | A. 지금 구조 유지 + 개선 | B. executor를 NautilusTrader로 교체 | C. 백테스트만 기존 프레임워크로, 실행은 자체 유지 |
|---|---|---|---|
| 내용 | 7절 통합, 체결 시점 모델, 그룹 균등 가중치, 버퍼, (선택) 변동성 목표 | 전략을 Nautilus `Strategy`로 다시 쓰고, executor 원장·안전장치를 Nautilus 구성으로 옮긴다 | C-1: vectorbt를 **교차검증 기준**으로만 사용 / C-2: Nautilus 백테스트 엔진으로 이전 |
| 작업량 | 20~35h | KIS 어댑터: 80~150h + 학습(InstrumentProvider: KRX 호가 단위·가격제한·세션, ExecutionClient: KIS 주문·체결조회 → Nautilus 이벤트 변환, 대사용 보고서, 토큰·호출 제한). IB 전환: 30~60h(어댑터는 있음, 미국 데이터·환율·세금 처리는 별도) | C-1: 10~20h / C-2: 40~70h(일봉 커스텀 데이터, KRX용 가상 거래소 규칙) |
| 장점 | 가장 작고, 지금 안전장치·테스트(818개)를 유지한다. 7절 문제를 직접 해결한다 | 백테스트·실전 같은 코드, 성숙한 주문 상태 기계와 대사를 얻는다 | C-1: 독립 엔진으로 손익 계산을 검산한다(EXECUTOR.md 6절의 남은 일). C-2: 나중에 B로 가는 길이 열린다 |
| 단점 | 주문 상태 기계·대사는 직접 키워야 한다 | **Nautilus v2가 RC 단계**(`python/pyproject.toml` 2.0.0rc8, v1은 MIGRATION_V2.md 기준 약 3개월 보안 패치만) → API 변동 위험. 하루 1회 일봉 전략에는 과한 구조. Rust/PyO3 의존. Docker용 IB Gateway를 쓰면 보안 원칙 4번과 충돌(직접 실행으로 피할 수 있음) | C-1: 우리 모델은 상태를 가진다(as-of 가중치, 단계) → 목표 비중 시계열은 결국 우리 코드가 만들어야 하고 vectorbt는 손익만 검산한다. C-2: 실행은 여전히 따로라서 **7절 문제가 그대로 남는다** |
| 국장(KIS) 적합도 | 높음 | 낮음 — KIS 어댑터가 없다(어댑터 목록에 한국 증권사 없음) | C-1 높음 / C-2 중간(KRX 규칙을 직접 정의해야 함) |
| 미장 적합도 | KIS 해외주식: 중간(Broker ABC에 시장·통화 개념 추가 필요, 모의에서는 **지정가만** 가능 — 9.1). IB: 낮음(어댑터를 직접 만들어야 함) | IB: 높음(어댑터 성숙). KIS 해외: 낮음 | C-1 높음(데이터만 있으면 됨) / C-2 IB 데이터 경로 있음 |

**IB 관련**: IB를 통해 KRX 개별주식을 거래할 수 있는지는 **확인하지 못했다**(Lean IB 모델의 기본 주식 시장은 USA). IB로 가면 국장(KIS)과 미장(IB)이 다른 브로커가 되므로, 어느 쪽이든 Broker 인터페이스 하나 뒤에 두 구현을 두게 된다.

**권장: A + C-1.** B는 (1) 미장으로 IB를 실제로 쓰기로 정하고 (2) Nautilus v2가 정식 릴리스되고 (3) A의 7절 통합을 마친 뒤에 다시 검토한다. 그때는 통합된 포지션 정책 함수를 Nautilus `Strategy` 안에서 그대로 부를 수 있다.

---

## 9. 한투 공식 저장소 대조 (commit `2077773`)

> 보안 원칙 6번(주문 API 호출 금지, 실전 주문 tr_id를 저장소에 두지 않음)에 따라 **실전용 주문 tr_id는 이 문서에 적지 않는다.** 모의투자용과 조회용만 적는다. 이번 작업에서 KIS API는 한 번도 호출하지 않았다.

### 9.1 해외주식 주문·잔고 API — 있다

| 기능 | 경로 | 모의 tr_id | 비고 |
|---|---|---|---|
| 주문(미국 매수/매도) | `/uapi/overseas-stock/v1/trading/order` | VTTT1002U / VTTT1001U | 거래소 코드 `OVRS_EXCG_CD`(NASD·NYSE·AMEX 등). **모의투자는 `00` 지정가만 가능**(샘플 docstring). 실전은 MOO·LOO·MOC·LOC 등 지원 |
| 정정·취소 | `order_rvsecncl` | VTTT1004U | |
| 잔고 | `inquire_balance` | VTTS3012R | |
| 체결기준 현재잔고 | `inquire_present_balance` | VTRP6504R | |
| 주문체결내역 | `inquire_ccnl` | VTTS3035R | |
| 미체결내역 | `inquire_nccs` | **모의 tr_id 없음**(샘플은 실전용 조회 tr_id만) | 모의에서는 미체결 확인을 주문체결내역으로 해야 한다 |

→ 미장을 KIS로 하면 브로커를 하나로 유지할 수 있다. 다만 지금 KisBroker의 시장가 전제는 그대로 옮길 수 없다(모의는 지정가만). 국내도 정정취소 가능 주문 조회(`inquire_psbl_rvsecncl`, TTTC0084R)는 실전용 조회 tr_id만 있어서, 모의에서는 체결조회(VTTC0081R)로 미체결을 확인하는 지금 방식이 맞다.

### 9.2 투자자별 매매동향 — `collectors/kis_client.py` TODO 대조

| 항목 | kis_client.py | 공식 샘플 | 결과 |
|---|---|---|---|
| 경로 | `/uapi/domestic-stock/v1/quotations/inquire-investor` | 같음 | 일치 |
| tr_id | `FHKST01010900` | `FHKST01010900`(실전·모의 같음) | 일치 |
| 파라미터 | `FID_COND_MRKT_DIV_CODE="J"`, `FID_INPUT_ISCD` | 같음 | 일치 |
| 날짜 필드 | `stck_bsop_date` | `stck_bsop_date`(주식 영업 일자) | 일치 |
| 순매수 필드 | `prsn_ntby_qty`·`frgn_ntby_qty`·`orgn_ntby_qty` | 같음(개인·외국인·기관계 순매수 **수량**) | 일치. 금액은 `*_ntby_tr_pbmn` |
| 응답 행 수(과거 며칠분) | 미정 | 샘플에 명시 없음 | **확인 필요**(실제 호출로만 확정) |
| (참고) 과거 이력용 API | 없음 | `investor-trade-by-stock-daily`, tr_id `FHPTJ04160001`, `FID_INPUT_DATE_1`(기준일) 파라미터. 샘플에는 실전/모의 구분 분기가 없다 | KSJ_FOREIGN_SELL을 백테스트하려면 과거 이력이 필요하다. 이 API가 후보다. 모의 지원 여부는 **확인 필요** |

같은 파일의 다른 TODO:

| 함수 | 현재 | 공식 샘플 | 결과 |
|---|---|---|---|
| `current_price` | `/quotations/inquire-price`, `FHKST01010100`, docstring에 "TODO: 공식 문서 확인 후" | 같음 | 일치. **docstring TODO만 오래됐다**(코드 수정은 이번 범위 밖) |
| `daily_prices` | `NotImplementedError` | `inquire-daily-price`(FHKST01010400), `inquire-daily-itemchartprice`(FHKST03010100) | yfinance로 대체됐으므로 필요성 낮음(CLAUDE.md 미완 목록과 같은 판단) |

### 9.3 국내 주문 유형(ORD_DVSN) — N-9 근거
모의계좌 Postman 샘플 설명: `00` 지정가, `01` 시장가, `02` 조건부지정가, `03` 최유리, `04` 최우선, `05` 장전 시간외, `06` 장후 시간외, `07` 시간외 단일가 등. 모의투자에서 05/06이 실제로 접수되는지는 **확인 필요**하다.

---

## 10. 가장 먼저 고쳐야 할 것 3가지

| 순서 | 무엇을 | 왜 먼저 | 대략 작업량 |
|---|---|---|---|
| 1 | **비중→수량 함수 통합 + 정확한 동치 테스트**(7.3) | 섀도 괴리가 자동 강등 기준인데, 지금은 그 값에 구현 차이가 섞여 있다. 이걸 고치기 전의 섀도 기록은 해석하기 어렵다. 버퍼(pst)도 여기서 같이 결정한다 | 8~12h |
| 2 | **체결 시점·주문 유형을 하나로 정하고 백테스트에도 같이 적용**(N-9) | 지금 가정(t 종가 결정 → t 종가 체결)은 시장가 주문으로 실현할 수 없다. 장후 시간외(`06`)는 거래량이 적고, t+1 시가는 수익률이 달라진다. 어느 쪽인지에 따라 what_if 결과가 바뀐다. **정하기 전에는 KisBroker로 모의 주문을 보내지 않는다** | 4~8h(+ 모의 계좌로 `06` 접수 확인) |
| 3 | **가중치를 고정 그룹 균등으로 바꾸고 임계값을 다시 정의**(6.2 안 1) | 지금 장치는 정보 없이 불연속성만 만든다. 1·2번 이후의 섀도 기록이 매주 바뀌는 가중치 탓에 흔들리지 않게 하려면, 섀도를 다시 시작하기 전에 고정해야 한다. 그룹 구분과 임계값 숫자는 **사용자가 결정**해야 한다 | 4~6h(결정 후) |

그 다음 후보: 폴링 간격과 미체결 처리(N-4), 주문 상태 전이 이벤트(N-2), 변동성 목표(P-8), vectorbt 교차검증(C-1).

---

## 11. 확인하지 못한 것

- vectorbt 라이선스(clone하지 않음).
- IB를 통한 KRX 개별주식 거래 가능 여부.
- KIS: 장 마감 후 시장가(`01`) 주문의 처리 방식, 모의투자의 `05/06` 지원, `inquire-investor` 응답 행 수, `investor-trade-by-stock-daily`의 모의 지원 여부. 전부 실제 호출로만 확정할 수 있다(이번에는 호출하지 않음).
- 작업량 추정은 코드를 읽고 낸 대략치이고, 프로토타입으로 검증하지 않았다.
