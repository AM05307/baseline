# 차이 분석 (2) — freqtrade · Lean · NautilusTrader · 한투 공식 저장소 대비

작성: 2026-10-19 · 읽는 사람: 이 저장소 개발자(본인) · **분석·문서 전용: 이 작업에서 코드는 바꾸지 않았다.**

같은 날 다른 세션이 쓴 [reference_gap_analysis.md](reference_gap_analysis.md)(이하 **문서 1**)가 있다. 문서 1은 pysystemtrade를
기준으로 가중치 최적화, what_if와 planner의 이원화, 방향 선택지(A/B/C)를 다룬다. 이 문서(**문서 2**)는 문서 1이 다루지 않은 것에
집중하고, 겹치는 결론은 문서 1의 절을 가리킨다. 문서 1과 결론이 다른 곳은 따로 표시했다(5절 끝).

- 참고 저장소는 읽기만 했다. 실행·빌드하지 않았고, **코드를 복사하지 않았다**(설계만 참고).
- 우리 코드의 동작은 2026-10-19 05:57 이후 작업 트리를 직접 읽어 확인했다. 그 시점에 이미 반영된 다른 세션의 변경도 포함된다.
  `analysis/position_policy.py`(비중→수량 통합)가 그 예다.
- 확인하지 못한 것은 "확인 필요"로 적고 6절에 모았다.
- **전략(신호) 품질은 이 분석의 범위가 아니다.** 신호가 좋은지가 아니라, 신호를 받아 집행하는 구조가 참고 프로젝트와 비교해
  어디가 비었는지를 본다. 전략 검증 **방법**만 4절에 따로 정리했다.

---

## 0. 핵심 발견 5가지

| # | 발견 | 위험도 |
|---|---|---|
| 1 | **재시작 복구가 없다.** 참고 프로젝트 셋(freqtrade·Lean·Nautilus)은 모두 시작할 때 브로커의 미체결 주문과 보유를 내부 상태에 맞춘다. 우리 executor는 보유 수량만 비교하고, 브로커 인터페이스에 미체결 주문 조회가 없다. 그래서 `planned`·`submitted`로 남은 주문을 다시 확인하지 않는다. 주문을 보낸 직후 프로세스가 죽으면 결과를 모르는 주문이 남는다. 지금은 다음 실행의 수량 대사가 이를 잡아 정지(halt)한다. 안전하게 멈추기는 하지만 복구는 전부 수동이다. | 높음(모의투자 전에) |
| 2 | **체결 시점을 실제로 가능한 방식으로 정해야 한다**(문서 1의 N-9와 같은 결론). 참고 프로젝트들은 백테스트에서도 "다음 봉 시가 체결"처럼 실제로 가능한 체결을 가정한다(freqtrade `docs/backtesting.md`). 우리는 "t일 종가에 결정하고 t일 종가에 체결"을 가정한다. 그런데 섀도는 장 마감 뒤에 돌고, KisBroker는 시장가(`01`)로 주문한다. 한투 주문구분에는 장후 시간외(`06`)·시간외 단일가(`07`)가 따로 있다. | 높음 |
| 3 | **"신호 → 위험 상태 → 목표 비중" 경로가 네 곳에 복사돼 있다.** 비중→수량 변환은 `position_policy`로 통합됐다(문서 1의 7절 1번은 처리됨). 하지만 그 앞 단계인 결정일 정렬 → as-of 가중치 → 점수 → 단계 계산이 `scripts/what_if_recent.build_model_frame`, `executor/shadow.model_states`, `scripts/debate_scorecard.risk_states_on`, `scripts/opportunity_by_risk_state`(앞의 첫 번째 것을 import)에 따로 있다. 참고 프로젝트는 같은 전략 코드를 백테스트·dry-run·실전이 그대로 공유한다. | 중간 |
| 4 | **보호장치가 "단계 강등"에 몰려 있다.** freqtrade는 손절 연속·최대낙폭·쿨다운·저수익 종목 잠금을 각각 독립 보호장치로 둔다. Nautilus는 주문 금액 상한·주문 속도 제한·거래 상태(ACTIVE/REDUCING/HALTED)를 RiskEngine에 둔다. 한투 strategy_builder도 일일 손실 한도를 둔다. 우리에게는 일일 손실 한도, 일일 주문 횟수 한도, 쿨다운이 없다. 하루에 한 번 도는 일봉 전략이라 당장은 낮지만, 모의투자를 매일 돌리기 시작하면 필요하다. | 중간 |
| 5 | **단일 종목(000660) 100% 구조는 참고 프로젝트들이 "기본으로 막는" 형태다.** freqtrade는 동시 보유 수(`max_open_trades`)와 거래당 금액으로 쪼개고, Lean은 섹터 노출 상한 모델이 있고, 한투 샘플도 종목당 최대 비중 20%가 기본이다. 우리 사다리는 **전체 손실 금액**을 단계 한도로 묶지만, **한 종목의 하루 급락**은 그대로 받는다(10/15 −11.06%, 8거래일 −30.9%). 막을지 말지는 설계 결정이라 사용자가 정할 일이다(5절 R-5). | 중간(설계 결정) |

---

## 1. 참고 저장소와 라이선스

`../refs/`에 shallow clone(`--depth 1`)했다. 날짜는 clone한 HEAD 커밋의 날짜다.

| 저장소 | 라이선스 | HEAD | 우리에게 주는 의미 |
|---|---|---|---|
| koreainvestment/open-trading-api | **LICENSE 파일 없음**. README에 "샘플 코드로 만든 프로그램의 손해는 책임지지 않음"이라는 면책만 있다 | `2077773` (2026-10-14) | 라이선스가 명시되지 않았으므로 기본적으로 저작권자가 권리를 모두 가진다고 봐야 한다. **코드 복사 불가.** API 사실(경로·tr_id·필드 이름)만 대조에 썼다(KisBroker·kis_client) |
| freqtrade/freqtrade | GPL-3.0 | `23eeadc` (2026-10-18) | 코드를 가져오면 우리 코드 전체가 GPL 의무를 진다. **설계만 참고.** 또한 암호화폐 거래소(ccxt) 전용이라 KRX에 그대로 쓸 수 없다 |
| QuantConnect/Lean | Apache-2.0 | `d4cac37` (2026-10-16) | 허용적 라이선스. 라이브러리나 엔진으로 써도 고지 의무만 있다 |
| nautechsystems/nautilus_trader | LGPL-3.0 | `6875261` (2026-10-18) | 라이브러리로 쓰는 것은 괜찮다. 라이브러리 자체를 고치면 그 부분은 LGPL로 공개해야 한다 |

---

## 2. 프로젝트별 전체 흐름

### 2.1 freqtrade (암호화폐 봇)

| 항목 | 내용 | 근거 |
|---|---|---|
| 흐름 | 거래소에서 OHLCV를 받는다(ccxt) → `IStrategy.populate_indicators/entry/exit_trend`로 신호를 만든다 → pairlist로 대상 종목을 고른다 → 거래당 금액(`stake_amount`)과 동시 보유 수(`max_open_trades`)로 크기를 정한다 → protections가 잠그면 진입하지 않는다 → 주문 → SQLAlchemy `Trade`/`Order` 모델에 기록 → Telegram·웹 UI로 모니터링 | `freqtrade/freqtradebot.py`, `freqtrade/persistence/` |
| 백테스트·실전 공유 | **같은 `IStrategy` 클래스**를 백테스트·dry-run·실전이 그대로 쓴다. 백테스트의 체결 가정은 문서로 공개한다: 진입은 시가, 같은 봉에서는 손절이 ROI보다 먼저, "저가가 고가보다 먼저" 등 | `docs/backtesting.md`(560행 근처 가정 목록) |
| 모의/드라이런 | `dry_run: true`이면 실제 주문 없이 가상 지갑(`dry_run_wallet`)으로 체결을 흉내 낸다. **DB 파일도 따로** 쓴다(`tradesv3.dryrun.sqlite`) | `freqtrade/constants.py`(`DEFAULT_DB_DRYRUN_URL`) |
| 보호장치 | `StoplossGuard`(손절 연속 시 잠금), `MaxDrawdown`(구간 낙폭 초과 시 잠금), `CooldownPeriod`(청산 뒤 재진입 대기), `LowProfitPairs`(수익이 낮은 종목 잠금). 운용 자금 한도는 `tradable_balance_ratio`(계좌의 몇 %까지)와 `available_capital`(봇의 시작 자본, 한 계좌에서 봇 여러 개를 돌릴 때) | `freqtrade/plugins/protections/`, `docs/configuration.md` 169–170행 |
| 주문 상태·재시작 | 주문을 DB에 남긴다. **시작할 때** `startup_update_open_orders()`가 DB의 미체결 주문을 거래소에서 다시 조회해 상태를 맞춘다. 5일 넘게 확인되지 않는 주문은 취소된 것으로 본다. 미체결 주문은 `unfilledtimeout`이 지나면 취소하고, 청산 주문은 새 가격으로 다시 낸다. 시간 초과 횟수가 쌓이면 긴급 청산한다. `cancel_open_orders_on_exit`는 종료 시 미체결을 취소한다 | `freqtradebot.py` 573행, `docs/configuration.md` 178·193–196행 |
| 설정·비밀 | `config.json` + `FREQTRADE__{섹션}__{키}` 환경변수로 덮어쓴다(예: 거래소 키). 공개 설정과 비밀 설정을 파일로 나누는 방식 | `docs/configuration.md` 27–36행 |

### 2.2 Lean (QuantConnect 엔진)

| 항목 | 내용 | 근거 |
|---|---|---|
| 흐름 | 프레임워크 모드에서 Universe Selection → Alpha(Insight) → Portfolio Construction(목표 비중) → Risk Management(목표 조정) → Execution(주문) 순서. 각 단계는 갈아 끼울 수 있는 모델이다 | `Algorithm.Framework/` |
| 백테스트·실전 공유 | **같은 알고리즘 코드**를 쓰고, 환경은 `config.json`의 `environment`(`backtesting`/`live-paper`/`live-*`)와 BrokerageModel로 바꾼다 | `Launcher/config.json` 9행 |
| 모의 | `live-paper` 환경 = 실시간 데이터 + `PaperBrokerage`(가상 체결) | `Brokerages/Paper/` |
| 보호장치 | 리스크 모델: 종목별·포트폴리오 최대낙폭, 섹터 노출 상한, 미실현 이익 실현, 트레일링 스톱 | `Algorithm.Framework/Risk/` |
| 주문 상태·재시작 | 실전 시작 시 `LoadExistingHoldingsAndOrders`가 브로커에서 **보유와 미체결 주문을 불러온다**(브로커가 기준). 실행 중에는 `PerformCashSync`로 현금을 주기적으로 대사하고, 정해진 횟수를 넘겨 실패하면 예외로 멈춘다 | `Engine/Setup/BrokerageSetupHandler.cs` 400행, `Engine/TransactionHandlers/BrokerageTransactionHandler.cs` 745행 |
| 설정·비밀 | `config.json` 한 파일(`api-access-token` 등). 브로커별 키도 같은 파일이나 환경 설정으로 넣는다 | `Launcher/config.json` |
| **국장 적합도** | 장 운영시간 DB(`Data/market-hours/market-hours-database.json`)에 KRX는 **선물·지수(`Future-krx-KM`, `Index-krx-KM`)만 있고 개별 주식 항목은 없다**. KRX 주식을 돌리려면 장 시간·휴장일 데이터를 직접 넣어야 한다. 한투 backtester가 `setup_lean_data.sh`로 Lean 데이터 환경을 따로 만드는 이유로 보인다(확인 필요) | 같은 파일 grep 결과 |

### 2.3 NautilusTrader

| 항목 | 내용 | 근거 |
|---|---|---|
| 흐름 | 데이터 클라이언트 → 메시지 버스 → Strategy/Actor → 주문 명령 → RiskEngine(검사) → ExecutionEngine → ExecutionClient(어댑터) → 체결 이벤트 → Cache·Portfolio. 모든 상태 변화가 이벤트다 | `docs/concepts/architecture.md`, `event_sourcing.md` |
| 백테스트·실전 공유 | 백테스트·sandbox·실전이 **같은 커널(`NautilusKernel`)**과 같은 전략·실행 알고리즘 코드를 쓴다. 차이는 문서 한 절에 모아 두었다 | `docs/concepts/overview.md` 15·39행, `architecture.md` 158행 |
| 모의 | `sandbox` = 실시간 데이터 + 가상 체결 | `architecture.md` 153행 |
| 보호장치 | RiskEngine이 주문 낼 때마다 검사한다: 가격·수량 정밀도, 주문당 금액 상한(`max_notional_per_order`), 현금 계좌 잔고 영향, **제출·수정 속도 제한**, 거래 상태. `REDUCING`(줄이는 주문만 허용)과 `HALTED`(취소·조회만 허용)가 있다 | `docs/concepts/execution/index.md` 185–210·282–283행 |
| 주문 상태·재시작 | 시작할 때와 실행 중에 **대사(reconciliation)**한다. 보관된 주문·포지션 상태를 거래소 보고서와 맞춘 뒤에야 전략을 시작하고, 실패하면 시작을 중단한다. 외부에서 생긴 주문·포지션도 처리 규칙이 있다 | `docs/concepts/live.md` 30·80–100·244행, `execution/reconciliation.md` |
| 설정·비밀 | 어댑터별 설정 객체(`ExecutionClientConfig` 등). 키는 환경변수로 받는 어댑터가 많다(어댑터마다 다름) | `docs/developer_guide/python_adapters.md` 89행 |
| **국장 적합도** | 공식 어댑터에 한국 증권사가 없다. 커뮤니티 어댑터에는 대만 SinoPac 하나가 있다. 직접 만들 수는 있다: **별도 파이썬 패키지**로 `ExecutionClientFactory`·`DataClientFactory`를 구현하면 된다. 단 v1→v2 이행 중이다(v2는 Rust 코어 + PyO3, v1은 이행 뒤 약 3개월 보안 패치만) | `ADAPTERS.md` 80–129행, `docs/concepts/adapters.md` 62–68행, `MIGRATION_V2.md` |

### 2.4 한투 공식 저장소 (strategy_builder + backtester)

| 항목 | 내용 | 근거 |
|---|---|---|
| 흐름 | 웹 UI에서 지표를 조합해 전략을 설계한다 → `.kis.yaml`로 내보낸다 → backtester가 **Lean(Docker)**용 코드를 생성해 과거 검증 → strategy_builder가 실시간 시그널을 만들고 모의/실전 주문 | `strategy_builder/README.md`, `backtester/README.md` 6·229–245행 |
| 백테스트·실전 공유 | **같은 코드가 아니라 같은 명세(YAML)**를 공유한다. 백테스트는 Lean용 생성 코드, 실전은 `strategy_builder/core/`의 별도 코드다. 우리 구조(what_if와 executor)와 같은 종류의 이원화를 안고 있다 | 같은 README |
| 모의 | `env_dv="demo"`(모의) / `"real"`. 주문 실행기의 기본값이 `"demo"`다 | `core/order_executor.py` 31행 |
| 보호장치 | `RiskManager`: 손절 −5%, 종목당 최대 비중 20%, 일일 손실 한도(원). **일일 손실은 메모리에만 있어서** 재시작하면 0으로 돌아간다 | `core/risk_manager.py` 22–110행 |
| 주문 상태·재시작 | 주문을 보내고 결과 DataFrame만 돌려준다. 주문을 영속 저장하거나 재시작 때 복구하는 코드는 `core/`에서 찾지 못했다. 중복 매수 방지는 보유 여부 확인뿐이고, 기본값은 중복 매수 허용(`allow_duplicate_buy=True`)이다 | `core/order_executor.py` 31·44–100행 |
| 설정·비밀 | `kis_devlp.yaml`에 실전 키(`my_app`/`my_sec`)와 **모의 키(`paper_app`/`paper_sec`)를 따로** 둔다. 토큰은 파일로 저장해 재사용한다. `_DEBUG=True`로 켜면 요청 헤더(앱키 포함)를 그대로 출력하고, 기본값은 `False`다. 로그 인젝션 방지 헬퍼(`log_sanitize.py`)가 있다 | `kis_devlp.yaml`, `examples_llm/kis_auth.py` 55·200–240·410–440행 |

---

## 3. 우리 시스템과 비교

### 3.1 항목별 비교

| 항목 | 우리 구현(2026-10-19 작업 트리) | 참고 프로젝트 | 차이 | 위험도 |
|---|---|---|---|---|
| 백테스트와 실행이 같은 로직을 쓰는가 — **비중→수량** | `analysis/position_policy.py` 하나를 what_if(`simulate_shares`)와 planner가 함께 쓴다(다른 세션이 통합) | 같은 전략 코드를 공유한다(freqtrade·Lean·Nautilus). 한투는 명세만 공유 | 통합됨. 남은 차이는 체결 시점(아래 행)뿐 | 낮음 |
| 백테스트와 실행이 같은 로직을 쓰는가 — **신호→단계→목표 비중** | 네 곳에 복사(0절 발견 3) | 한 전략 클래스 | 한쪽만 고치면 섀도·백테스트·채점 숫자가 소리 없이 어긋난다 | 중간 |
| 체결 시점·주문 유형 | 백테스트·섀도: t일 종가 체결. KisBroker: 시장가 `01`, 장 마감 뒤 실행 | freqtrade: 다음 봉 시가. Nautilus·Lean: 체결·지연 모델과 BrokerageModel | 실제로 가능한 체결 방식과 백테스트 가정이 다르다 | 높음 |
| 주문 상태 관리 | `orders`(멱등 키) + 상태 갱신, 부분 체결 기록, 장중 미체결은 당일 취소 | 셋 다 주문 상태 기계 + 이벤트 | 상태 전이 이벤트가 없고, 결과를 모르는 주문(unknown outcome)을 다룰 수 없다 | 중간 |
| 재시작 복구 | 없음. 보유 수량 대사만 하고, 불일치면 정지 | freqtrade `startup_update_open_orders`, Lean `LoadExistingHoldingsAndOrders`, Nautilus 시작·실행 중 대사 | 미체결 주문 조회·복구 경로 자체가 없다 | **높음**(모의투자 전) |
| 중복 주문 방지 | (전략, 종목, 결정일, 계좌) 키 — 재시작해도 같은 주문을 두 번 보내지 않는다 | freqtrade: DB의 열린 주문 / 한투: 보유 확인만 | 우리가 더 엄격하다 | 낮음 |
| 모의/드라이런 구조 | shadow(원장 안 종가 체결) · paper(KisBroker, 모의 URL만) · 계좌별 원장 분리 | freqtrade: dry-run 전용 DB / Lean: live-paper / Nautilus: sandbox | 비슷하다. 우리 섀도는 실시간 데이터가 아니라 장 마감 뒤 일봉이다(일봉 전략이라 문제없음) | 낮음 |
| 보호장치 | 단계 강등(MDD·괴리·주문 오류·불일치) → 수동 판단·정지, 킬 스위치(파일+DB, 주문마다 확인), 한도 초과 계획 거부, 연속 cap_rejected | freqtrade 보호장치 4종, Nautilus RiskEngine(금액·속도·거래 상태), 한투 손절·종목 비중·일일 손실 | 일일 손실 한도·일일 주문 횟수·쿨다운이 없다. REDUCING(줄이는 주문만 허용) 상태가 없다 | 중간 |
| 단일 종목 집중 | 000660 하나에 운용분 100%까지. 전체 손실 금액만 단계 한도로 묶는다 | freqtrade: 종목 여러 개 + 동시 보유 수 + 거래당 금액 / Lean: 섹터 노출 상한 / 한투: 종목당 20% | 종목 하나의 급락을 그대로 받는다. 단 운용분 자체가 계좌의 일부(사다리)라서 계좌 전체로 보면 집중도가 낮다 | 중간(설계 결정) |
| 운용 자금 한도 | 운용분(슬리브) 원장 + 단계 한도, 한도 밖 현금 분리 | freqtrade `available_capital` + `tradable_balance_ratio` | 같은 생각이다. 우리는 승격·강등으로 한도가 바뀌는 것까지 있다 | 낮음 |
| 설정·비밀 | `.env`(실전)과 `.env.paper`(모의) 분리, 시작 단계 거부(모의 URL·키 섞임), 마스킹, 토큰 메모리만, 주문 엔드포인트 정적 검사 | 한투: yaml에 실전·모의 키 함께, 토큰 파일 저장 / freqtrade: 환경변수 덮어쓰기 | 우리가 더 엄격하다. 다만 토큰을 메모리에만 두므로 실행할 때마다 새 토큰을 받는다 — **KIS 토큰 발급 빈도 제한과 부딪히는지 확인 필요** | 낮음 |
| 데이터 출처 | 시세는 yfinance(000660.KS), 수급은 KIS(미연결) | 브로커·거래소 데이터 | 비공식 데이터로 공식 브로커 주문을 계산한다. 수정주가·누락을 브로커 데이터로 교차 확인하지 않는다 | 중간 |
| 거래소 규칙 | 휴장일 캘린더(2026, 데이터로 검증), 주문에 호가 단위·가격제한·접수 시간 반영 없음 | Lean SymbolProperties·장 운영시간 DB, Nautilus InstrumentProvider | 시장가만 써서 지금은 호가 단위 문제가 없다. 지정가·시간외로 바꾸면 필요하다 | 중간 |
| 기록·모니터링 | append-only 원장·사건·단계 이력, 섀도 실행 시도마다 사유, executor 자체 화면 | freqtrade Telegram·웹 UI, Lean 결과 핸들러, Nautilus 이벤트 소싱 | 알림이 없다(화면을 열어야 안다) | 낮음 |

### 3.2 갖춘 것 / 빠진 것 / 과하게 만든 것

문서 1의 5절과 겹치는 항목은 적지 않았다. 아래는 **문서 2에서 새로 나온 것**이다.

**이미 갖춘 것**
- 멱등 주문 키는 한투 샘플보다 엄격하고, freqtrade의 "DB의 열린 주문" 방식과 같은 목적을 이룬다.
- 운용분(슬리브)과 한도 밖 현금을 나누는 방식은 freqtrade의 `available_capital`과 같은 생각이다.
- 실전/모의 설정 분리와 시작 단계 거부는 한투 샘플(한 yaml에 둘 다 둠)보다 안전하다.

**빠진 것**

| 빠진 것 | 참고 | 위험도 |
|---|---|---|
| 시작 시 미체결 주문 복구(브로커 인터페이스에 미체결 주문 조회 추가 + `planned`/`submitted` 행 재확인) | freqtrade `startup_update_open_orders`, Lean `LoadExistingHoldingsAndOrders`, Nautilus 대사 | 높음 |
| 결과를 모르는 주문의 처리 규칙(보낸 직후 죽은 경우) | Nautilus submission recovery | 높음 |
| 일일 손실 한도(재시작해도 유지되게 DB에) · 일일 주문 횟수 한도 | 한투 RiskManager(메모리, 재시작 시 초기화 — **이 점은 따라 하지 말 것**), Nautilus 속도 제한 | 중간 |
| 강등 뒤 쿨다운(재승격 최소 대기) | freqtrade `CooldownPeriod` | 낮음 |
| REDUCING 상태(줄이는 주문만 허용) — 지금은 정지(halt·review_required)와 정상 둘뿐 | Nautilus TradingState | 중간 |
| 알림(정지·강등·섀도 건너뜀) | freqtrade Telegram | 낮음 |
| 신호→단계 경로 단일화 | 셋 다 한 전략 코드 | 중간 |

**과하게 만든 것** (문서 1의 표에 추가)

| 항목 | 이유 |
|---|---|
| 관점별 토론(LLM 5관점 2라운드) | 매매에도 위험 점수에도 반영하지 않고 섀도 기록만 하는 장치에 호출 10회와 테이블 3개(memos·debate_shadow·debate_review)가 들어갔다. 비용 상한과 수동 실행으로 묶어 두어 위험하지는 않다. 하지만 채점에 필요한 표본 60건이 쌓이려면 매일 돌려도 석 달이 걸린다. 그 전까지는 정보가 없다 |
| 채점 스크립트 여러 개 | `signal_scorecard`·`opportunity_by_risk_state`·`risk_score_history_report`·`what_if_recent`·`debate_scorecard`가 각자 위험 상태를 다시 계산한다. 0절 발견 3과 같은 원인이다 |

---

## 4. 참고 프로젝트들의 전략 검증 방법 (전략 품질은 범위 밖)

| 방법 | freqtrade | Lean | Nautilus | 한투 backtester | 우리 |
|---|---|---|---|---|---|
| 미래참조 탐지 | **`lookahead-analysis`**: 같은 전략을 잘린 데이터로 다시 돌려 신호가 바뀌는지 본다. **`recursive-analysis`**: 지표의 초기값 의존성을 본다 | 엔진 구조로 막는다(시간 순 데이터 공급) | 결정적(deterministic) 이벤트 순서 | 문서에서 찾지 못함 | 함수 단위 테스트("t 이후를 바꿔도 t까지 결과 불변"), 해외 세션 정렬 |
| 파라미터 탐색 | hyperopt(손실 함수 Sharpe·Sortino·Calmar 등). 과적합 경고가 문서 곳곳에 있다(소수 셋째 자리 제한 등) | Optimizer: Grid·Euler 탐색 | (엔진 밖) | Grid/Random 탐색 | **의도적으로 하지 않는다**(기본값만, 과최적화 방지) |
| 표본 외 검증 | `--timerange`로 구간을 나눠 직접 수행 | 날짜 범위로 직접 수행 | 직접 수행 | 확인 못 함 | 최근 1년 OOS 분리, as-of 가중치로 walk-forward |
| walk-forward | **내장 기능 없음**(문서·코드 grep) | 엔진 저장소에서 전용 모듈을 찾지 못함 | 찾지 못함 | 찾지 못함 | 월별 as-of 재계산(백필) |
| 통계적 유의성 | 없음 | 보고서 통계(Sharpe 등) | 보고서 | 수익률 지표 | 기준률 대비 이항검정·부트스트랩 CI·BH 다중비교 보정, 비겹침 표본 수 |
| 체결·비용 현실성 | 체결 가정을 문서로 공개, 수수료 반영 | Fill·Slippage·Fee 모델 | 체결·지연(latency) 모델 | Lean 모델 | 수수료·세금(스케줄)·슬리피지 인자(기본 0). 체결 시점은 0절 발견 2 |

**정리**: 통계 검증(유의성·다중비교·walk-forward)은 우리가 참고 프로젝트보다 앞선다. 문서 1의 6절은 "그 결과를 가중치에 자동으로 넣는 연결"이 문제라고 본다. 반대로 비어 있는 것은 두 가지다.
1. **파이프라인 전체를 대상으로 하는 미래참조 탐지 도구**(freqtrade `lookahead-analysis` 방식). 지금 테스트는 함수 하나씩만 본다. 섀도 실행기 전체를 "t까지 자른 DB"와 "전체 DB"로 돌려 t일 결정이 같은지 비교하는 하네스가 없다.
2. **체결 가정의 민감도**: 같은 신호로 "t 종가 / t+1 시가 / 장후 시간외 종가" 체결을 각각 백테스트해서 차이를 보는 표가 없다.

---

## 5. 개선 로드맵 제안 (위험도 높은 순 — 제안만, 구현은 고른 뒤에)

각 항목에 "프레임워크 도입 vs 우리 코드 수정" 의견을 달았다. 작업 시간은 대략치다.

| 순위 | 항목 | 프레임워크 vs 우리 코드 | 근거 | 대략 |
|---|---|---|---|---|
| R-1 | **체결 시점 결정 + 백테스트 일치**: 장후 시간외(`06`) 종가 / 시간외 단일가(`07`) / t+1 시가(`01`) 중 하나를 고르고, `position_policy`·`what_if`·섀도가 같은 가정을 쓰게 한다. 4절의 민감도 표를 먼저 만들어 선택 근거로 쓴다 | **우리 코드** | KRX 시간외 주문 규칙은 어느 프레임워크에도 없다. Lean은 KRX 주식 장 시간 데이터조차 없다 | 6–12h |
| R-2 | **재시작 복구**: `Broker`에 미체결·당일 주문 조회를 추가한다(KIS는 `inquire-daily-ccld`로 가능, 이미 KisBroker 안에 있음). 엔진 시작 시 `planned`·`submitted` 행을 브로커에서 다시 확인해서, 체결이면 원장에 반영하고 미체결이면 취소한다. 결과를 모르면 정지한다 | **우리 코드**(freqtrade `startup_update_open_orders`의 순서를 설계로만 참고) | 하루 한 번, 주문 몇 건 규모에 상태 기계 프레임워크는 과하다. 필요한 것은 "시작 시 한 번 맞추기"다 | 6–10h |
| R-3 | **신호→단계→목표 비중 단일화**: `analysis/`에 `risk_states_for_dates(...)` 같은 순수 함수 하나를 두고 네 곳이 import한다. 동치 테스트로 고정한다 | **우리 코드** | 문서 1 7.3과 같은 방향(이번엔 결정 쪽) | 3–5h |
| R-4 | **보호장치 보강**: 일일 손실 한도·일일 주문 횟수(DB에 기록해서 재시작해도 유지), REDUCING 상태(강등 뒤 "보유 유지"와 "정리" 사이에서 매도만 허용), 강등 뒤 재승격 쿨다운 | **우리 코드**(freqtrade protections와 Nautilus TradingState를 개념만 참고) | 지금 정지 장치(halt·review_required)를 확장하는 작은 변경이다 | 5–8h |
| R-5 | **단일 종목 집중 정책 결정(사용자)**: (a) 지금대로 두기(손실은 단계 한도로만 묶음), (b) 종목당 최대 비중(예: 운용분의 x%)을 두고 나머지는 현금, (c) 나머지를 지수 ETF(069500)로 채우기. (c)는 매매 대상이 늘어나는 것이라 원칙 확인이 필요하다 | 결정 후 **우리 코드**(`position_policy`에 상한 하나 추가) | Lean 섹터 노출 상한, 한투 20%, freqtrade 분산이 참고. 숫자는 사용자가 정할 일 | 결정 후 2–4h |
| R-6 | **데이터 교차 확인**: KIS 키를 받으면 일봉을 KIS에서도 받아 yfinance와 날짜별로 비교한다(자동 보정 없음, 불일치 기록만). 신선도 검사에도 연결한다 | **우리 코드** | 실행 브로커의 데이터로 계산을 맞추는 것은 참고 프로젝트 공통 구조다 | 4–6h |
| R-7 | **파이프라인 미래참조 하네스**(4절 1번): 섀도 결정을 "t까지 자른 DB 사본"과 "전체 DB"로 각각 계산해 같은지 본다 | **우리 코드**(freqtrade lookahead-analysis 아이디어) | 함수 단위 테스트가 놓치는 배선 오류를 잡는다 | 3–5h |
| R-8 | **독립 엔진 손익 검산** | 문서 1의 C-1(vectorbt를 오라클로)과 같은 의견 | — | 문서 1 참고 |
| R-9 | 알림(정지·강등·섀도 건너뜀) | 우리 코드(나중) | 화면을 열지 않으면 모른다 | 2–4h |

### 5.1 executor 기반을 프레임워크로 바꾸는 경우

| | NautilusTrader | Lean | freqtrade |
|---|---|---|---|
| KIS 어댑터 | **직접 만들어야 한다.** 별도 파이썬 패키지로 `ExecutionClient`(주문·취소·체결조회 → Nautilus 이벤트), `DataClient`(일봉), `InstrumentProvider`(호가 단위·가격제한·세션)를 구현한다. 시작 대사에 쓸 보고서(미체결·체결·보유)도 만들어야 한다 | **직접 만들어야 한다(C#).** Brokerage 플러그인과 데이터 공급자를 만들고, KRX 주식 장 운영시간·휴장일 데이터를 장 시간 DB에 추가해야 한다 | **불가.** ccxt(암호화폐 거래소) 전용이라 KRX 주식 브로커를 붙이는 구조가 아니다 |
| 얻는 것 | 성숙한 주문 상태 기계, 시작·실행 중 대사, RiskEngine(금액·속도·거래 상태), 같은 코드로 백테스트·sandbox·실전 | 프레임워크 모델 분리(Alpha·Portfolio·Risk·Execution), 리스크 모델, 결과 보고서. 한투 backtester가 이미 Lean을 백테스트용으로 쓰고 있다 | (개념) 보호장치 4종, dry-run DB 분리, 시작 시 미체결 복구 순서 |
| 잃거나 드는 것 | v2가 아직 이행 중이라 API가 바뀔 위험이 있다(문서 1 8절: 2.0.0rc). 하루 1회 일봉 전략에는 무겁다. 지금 executor의 안전장치(사다리·킬 스위치·마스킹·정적 검사)와 테스트를 Nautilus 구성으로 옮겨야 한다 | 런타임이 .NET + Docker다. Docker로 키를 다루는 배포는 보안 원칙 4번(배포 안 함)과 부딪힌다. 파이썬 전략도 Lean 런타임 안에서 돈다 | GPL-3.0: 코드를 가져오면 저장소 전체에 GPL 의무가 생긴다 |
| 권장 | 지금은 아님. 조건: (1) R-1·R-2를 우리 코드로 해 보고도 상태 관리가 감당이 안 될 때 (2) v2 정식 릴리스 (3) 미장 IB를 실제로 쓰기로 할 때(IB 어댑터는 있음) | **백테스트 교차검증용으로만** 검토할 가치가 있다(한투 backtester가 이미 Lean+YAML 경로를 제공). 실행 기반으로는 권하지 않는다 | 실행 기반으로는 불가. 보호장치·복구 순서를 설계 참고로만 쓴다 |

**종합 의견**: 빠진 것(R-1·R-2·R-4)은 "프레임워크가 있어야만 풀리는 문제"가 아니다. 하루 한 번, 종목 하나, 주문 몇 건 규모에서 필요한 상태 관리는 **시작 시 한 번 맞추기 + 정해진 체결 방식**이 전부다. 이 정도는 우리 코드로 고치는 편이 작고, 지금 안전장치와 테스트(881개)를 그대로 지킨다. 문서 1의 권장(A + C-1)과 같은 방향이다.

### 5.2 문서 1과 결론이 다른 곳

- **재시작 복구의 위험도**: 문서 1은 "주문 상태 전이 이벤트·unknown outcome·체결 내역 조회"를 **중간**으로 봤다. 이 문서는 시작 시 복구를 **높음**(모의투자 전 필수)으로 본다. freqtrade·Lean·Nautilus 셋이 모두 시작 시 복구를 기본 경로로 갖고 있고, 우리는 지금 "불일치면 정지"로만 막기 때문이다. 정지 뒤 복구는 전부 수동이다.
- **비중→수량 이원화**: 문서 1은 이것을 가장 큰 위험으로 꼽았다. 작성 시점 이후 `position_policy.py`로 통합돼서, 이 문서에서는 **낮음**으로 내렸다. 남은 것은 결정 쪽 이원화(R-3)다.

---

## 6. 확인하지 못한 것

- KRX 장 마감 뒤 시장가(`01`) 주문이 모의투자에서 거부되는지, 다음 날 시가로 넘어가는지(KIS 문서나 모의 호출로 확인 필요). R-1의 전제다.
- KIS 접근 토큰의 발급 빈도 제한과 유효 시간. KisBroker는 토큰을 메모리에만 두므로 실행마다 새로 받는다.
- 한투 backtester가 KRX 휴장일·장 시간을 Lean에 어떻게 넣는지(`setup_lean_data.sh` 내용을 읽지 않음).
- Lean에서 walk-forward를 알고리즘 안에서 구현하는 공식 예제가 있는지(엔진 저장소 grep으로는 찾지 못했다. QuantConnect 문서 사이트는 보지 않음).
- Nautilus 커뮤니티 SinoPac(대만) 어댑터의 구조. KIS 어댑터 작업량을 추정할 실제 사례가 될 수 있다(보지 않음).
- freqtrade의 `dry_run` 기본값(설정 템플릿을 보지 않음).
