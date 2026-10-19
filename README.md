# baseline

주식 시장 데이터를 수집하고 **관계를 측정**하는 개인 대시보드.

> **개인 학습·포트폴리오용 프로젝트입니다. 투자 권유가 아니며, 이 코드를 사용해 생긴 손실에 대해 작성자는 책임지지 않습니다.**
> 자세한 내용은 맨 아래 [면책](#면책).

## 프로젝트 소개

감이나 전문가 말이 아니라 **데이터에서 나온 기준선**으로 시장을 관찰하기 위한 도구다. 수집 → 신호 계산 → 통계 검증 →
위험 단계 → (가상) 집행까지를 한 파이프라인으로 만들고, 각 단계가 "좋아 보이는 숫자"를 만들지 못하게 막는 장치를 먼저 설계했다.

### 아키텍처

- `collectors/` — yfinance 시세, 한국투자증권(조회 전용)·DART 수집 → SQLite(`baseline.db`), 멱등 upsert
- `analysis/` — DB·API를 모르는 순수 함수: 신호 16개, 이벤트·규칙 백테스트, 통계 검증(기준률·부트스트랩 신뢰구간·
  Benjamini-Hochberg 다중비교), 신호 그룹 위험 점수(기준 기간 백분위 단계), 비중→수량 정책, 국장/미장 비교 시뮬레이션
- `web/` — FastAPI 대시보드(GET만) · `mcp_server/` — LLM 에이전트용 조회 툴 · `agents/` — 가드레일이 있는 리서치·관점별 토론(섀도 기록만)
- `executor/` — 별도 DB의 실행 계층: 자본 사다리(수동 승격·자동 강등), 섀도 실행기, 모의투자 전용 브로커, 재시작 복구, 일일 한도
- 의존 방향은 `executor → analysis·db` 한쪽뿐이다(반대 방향 import는 정적 테스트로 막는다)

### 테스트

- `pytest -q` — 969 passed, 4 skipped (2026-10-19 기준 · 건너뛰는 4개는 환경변수로만 켜는 실서버 호출 테스트)
- 미래참조 회귀 테스트("t 이후 데이터를 바꿔도 t까지의 결과는 그대로"), 백테스트와 섀도 집행의 수량 경로 정확 일치,
  대시보드 렌더링의 nan/None 누수 검사, 주문 직후·부분 체결 중 프로세스 종료 → 재시작 복구 시나리오

### 안전장치

- **비밀값**: `.env` 커밋을 막는 pre-commit 훅, 모든 예외 메시지의 키·토큰·계좌번호 마스킹, 토큰은 메모리에만, 실전·모의 설정 분리와 시작 단계 거부
- **주문**: 모의투자 서버 전용(실전 URL·실전 주문 코드는 저장소에 없음을 정적 검사), 주문 하나하나 직전 킬 스위치(파일·DB) 확인
- **운영**: 잔고 불일치면 전면 정지, 결과를 모르는 주문은 브로커 조회로만 확정(애매하면 정지·사람 판단), 일일 손실률·주문 횟수·
  강등 뒤 쿨다운, 종목당 비중 상한(지수 ETF 예외)
- **자금 이동 없음**: 이체·출금 메서드가 없음을 정적 테스트로 확인. 실제 운용 금액 같은 개인 설정은 `config/*.local.yaml`(gitignore)에만

## 설계 원칙

이 프로젝트는 "예측"을 하지 않는다. 다만 **백테스트로 엄격하게 검증된 규칙만**
매수/목표/손절 기준가를 보여줄 수 있다 — 이것도 전망이 아니라 "이 기계적 규칙을
오늘 적용하면 나오는 레벨"이라는 측정값이다 (2026-10-06 갱신, 아래 1번 참고).

1. **측정하되 전망하지 않는다** — 상관계수·베타·승률은 계산한다. "오를 것 같다"는
   출력하지 않는다. 예외는 `analysis/price_rule_backtest.passes()`를 통과한
   규칙(거래 100건↑·전체·최근1년 거래당평균 둘 다 양수)의 매수/목표/손절
   기준가뿐 — 그 가격도 전망이 아니라 기계적 계산 결과다. LLM이 만드는
   서술(에이전트 요약 등)에는 이 예외가 적용되지 않는다 — 방향성 판단·목표가·
   확률 narrative는 여전히 전부 금지다.
2. **표본 수를 항상 보여준다** — 승률 65%는 n=20일 때와 n=500일 때 의미가 전혀 다르다. 화면에서 이를 숨기지 않는다.
3. **최대손실을 승률과 같은 크기로 표시한다** — 좋아 보이게 만드는 것이 목적이 아니다.
4. **실제 자금 매매 기능을 넣지 않는다** — baseline(수집·분석·대시보드)은 조회 전용이다. 실행 계층(`executor/`)은
   섀도(가상)와 한국투자증권 **모의투자 서버** 주문까지만 있고, 실전 계좌로 주문하는 경로는 없다. 매수/목표/손절
   기준가를 보여줘도 그 가격으로 주문이 나가는 경로는 없다.
5. **전문가 발언은 신호가 아니라 기록** — 예측을 저장하고 나중에 채점한다. 따르기 위해서가 아니라 적중률을 알기 위해서다.

## 프로젝트 핵심

- **외부 금융 API 연동 ETL 파이프라인** — 재시도, 멱등 쓰기(같은 배치를 여러 번 돌려도 중복 없음), 호출 간격 제한(rate limit) 대응
- **MCP 서버** — LLM 에이전트에 조회 전용 데이터 인터페이스를 제공(매매 기능 없음)
- **백테스트** — 표본 수와 최대손실을 항상 함께 표시해, 적은 표본·좋아 보이는 승률만 보고 과적합된 해석을 하지 않게 함
- **프롬프트 가드레일** — 예측성 출력(방향 단정·목표가·확률 서술)을 시스템 프롬프트와 출력 검사 두 겹으로 차단

## 비교 백테스트 핵심 결론

`scripts/market_compare.py` → `reports/market_compare.md` (2007-01 ~ 2026-10, 원화 기준, 가격 수익률·배당 미포함, 계산용 가정 금액).

| 대상 | 단순 보유 연환산 / MDD | 위험점수 비중 조절 연환산 / MDD |
|---|---|---|
| 000660 | 20.3% / −85.1% | 19.3% / −83.8% |
| 069500 | 8.4% / −41.3% | 8.3% / −40.1% |
| SPY | 11.0% / −30.6% | 7.7% / −30.6% |
| QQQ | 17.4% / −30.8% | 12.3% / −39.6% |

- **위험점수 비중 조절은 네 대상 모두 단순 보유보다 수익이 낮았다**(시작 연도 19개를 바꿔 봐도 모든 해에서 낮음).
- **최대낙폭은 대상에 따라 달랐다**: 000660·069500은 조금 줄었고, SPY는 같았고, QQQ는 오히려 커졌다.
  000660의 검증 기간(2019~, 기준점을 정한 뒤의 표본 외 구간)만 보면 최대낙폭이 −56.8% → −42.0%로 줄었지만
  수익도 +2002.5% → +1826.8%로 낮았다(`scripts/group_score_report.py`).
- 어느 시장·전략이 낫다는 결론이 아니다. 과거 한 구간의 측정이며 거래비용·세금·환율 값은 참고용 가정이다.

## 구조

```
baseline/
├── collectors/          데이터 수집 (스케줄 실행)
│   ├── collect_daily.py     us_market+kr_market+global_markets+news 통합 진입점
│   │                        (서로 독립 실패, --only로 일부만 선택 실행, --dry-run 지원)
│   ├── us_market.py        SOX·NVDA·MU·TSM·AMD·VIX (yfinance, 키 불필요)
│   ├── kr_market.py         000660·005930·코스피(KRX)·SKHY(ADR)·USDKRW (yfinance, 키 불필요)
│   ├── global_markets.py    코스닥·S&P500·다우·Euro Stoxx50·Sensex·Bovespa·베트남(ETF
│   │                        대리)·BIST100·은·IPO ETF·미국10년물 — 전문가 경험칙
│   │                        신호(analysis/signals.py)용 주변부 지표
│   ├── news.py               뉴스 헤드라인 (yfinance, 키 불필요)
│   ├── kis_client.py        한국투자증권 API (조회 전용, 수급 데이터 전용)
│   └── dart_client.py       OpenDART 전자공시 API (조회 전용, 재무제표·공시목록).
│                             DART_API_KEY 없으면 네트워크 전혀 안 탐 — collect_daily.py
│                             기본 실행에는 안 들어있음(--only dart로만 실행)
├── analysis/            순수 함수. DB도 API도 모른다
│   ├── calendar_rules.py   만기일·자사주 기간·실적일
│   ├── correlation.py      상관계수·시차·베타·괴리 탐지
│   ├── backtest.py         이벤트 백테스트
│   ├── adr.py               ADR-KRX 프리미엄 (전환비율 1:10)
│   ├── dart_financials.py   DART 재무계정 -> 공시일 기준 as-of PBR/영업이익률
│   │                        시계열(미래참조 금지, 분기 단독값 역산)
│   ├── sentiment.py         VIX 기반 시장 심리 게이지
│   ├── frameworks.py        투자 프레임워크 렌즈 (실존 인물 발언 재현 안 함)
│   ├── indicators.py        SMA·RSI(Wilder)·볼린저밴드·MACD
│   ├── strategies.py        지표 기반 보유 규칙 5개 (ma_cross·rsi_reversion·
│   │                        bollinger_reversion·macd_cross·ensemble). 파라미터
│   │                        최적화 안 함 — 기본값만 사용(과최적화 방지)
│   ├── rule_backtest.py     규칙 백테스트 엔진. t일 종가 신호 → t+1일 시가 체결,
│   │                        거래비용 반영, in-sample/out-of-sample 분리
│   ├── pullback.py          눌림목 지정가 매매 백테스트. 지정가 체결·손절/익절
│   │                        (일봉 저가/고가 기준)·max_hold_days 강제청산을
│   │                        직접 상태기계로 구현, 장세별(상승추세/박스권/
│   │                        하락추세) 성과+buy&hold 기회비용 비교 포함
│   ├── price_rules.py       매수/목표/손절 기준가 규칙 3개(이동평균 눌림목·
│   │                        ATR 밴드·전일 고저 돌파). 순수 계산, 검증은 안 함
│   ├── price_rule_backtest.py  위 규칙들의 백테스트 + "화면에 가격을 보여줘도
│   │                        되는가" 통과 판정(거래 100건↑·전체·최근1년
│   │                        거래당평균 둘 다 양수 — CLAUDE.md 원칙 1 갱신판)
│   ├── signals.py            전문가 경험칙 신호 16개(전문가 A·전문가 B·전문가 C) —
│   │                        on/off를 기계적으로 계산만 한다, 예측 아님
│   ├── signal_backtest.py    위 신호들의 과거 적중률·표본수 측정(5/20/63거래일,
│   │                        이벤트 묶음, 최근1년 out-of-sample 분리)
│   ├── weighting.py          적중률만큼 가중치 부여 (신호 30%·전문가 40% 상한,
│   │                        water-filling 재배분·정규화)
│   ├── risk_score.py         켜진 신호 가중치로 위험 점수(0~100)·오늘의 상태
│   │                        계산. 주문 없음(원칙 2)
│   └── checklist.py          "매수 전 체크" 정적 체크리스트(원문 인용 아님)
├── db/                  SQLite. 모든 쓰기는 멱등
├── web/                 FastAPI 대시보드 — 조회 전용, KIS 키 없이도 동작
│   └── charts.py            서버사이드 SVG 차트 (JS 라이브러리 미사용)
├── mcp_server/          MCP 서버 — 조회 전용 툴 12개, LLM 에이전트용 인터페이스
├── agents/
│   └── orchestrator.py      LLM 에이전트 tool-use 루프 (anthropic 패키지 비의존 — 덕 타이핑)
├── scripts/
│   ├── add_claim.py         전문가 발언·소문 기록 CLI (소속·매수/중립/매도 포함)
│   ├── grade_claims.py      기록된 발언·소문 사후 채점 CLI (API 키 불필요)
│   ├── run_agents.py        에이전트 배치 실행 CLI (Anthropic API를 호출하는 유일한 지점)
│   ├── backtest_strategies.py  기술적 지표 규칙 백테스트 CLI (과거 데이터 측정만,
│   │                            매매 실행 없음). --strategy all(기본)|pullback|price_rules
│   ├── seed_signals.py      experts/signals 테이블에 신호 정의 등록(멱등)
│   ├── seed_analyst_views.py  2026-07~08 발언 기록 1회성 시딩(append-only, 재실행 금지)
│   ├── seed_disclosure_claims.py  DART 공시를 claims에 시딩(confidence='official',
│   │                        원문 링크 포함, 멱등 — 같은 공시 중복 기록 안 함)
│   ├── add_manual.py        금리 단계·마이크론 매출총이익률·000660 선행 PBR·
│   │                        이벤트 날짜 수동 입력(append-only, 입력 이력 보존)
│   ├── compute_signals.py   analysis/signals.py를 실제 데이터로 돌려 signal_values 채움(멱등)
│   ├── recompute_weights.py  신호 가중치 재계산 + weight_history 기록(매주 일요일 권장)
│   ├── signal_scorecard.py  전체 백테스트 → reports/signal_scorecard.md 생성
│   ├── decompose_pullback.py  pullback 전략 손실 원인 분해(비용/복리/꼬리손실)
│   ├── run_weekly.py        collect_daily → compute_signals → recompute_weights 체이닝
│   │                         (서브프로세스 순차 호출, 한 단계 실패 시 중단, --dry-run 지원)
│   ├── risk_score_history_report.py  과거 위험 점수 재구성 + 점수 구간별 이후 수익률 분포
│   └── verify_outlier.py    data/manual/의 공식 KRX CSV와 DB 값 대조(자동 보정 없음) +
│                             --scan-limits(날짜별 가격제한 테이블 기준 이상치 스캔)
└── tests/
```

## 시작하기

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# .env 유출 방지 pre-commit 훅 활성화 (최초 1회 — .env에 실전 키가 있다고 가정하고 필수로 켠다)
git config core.hooksPath .githooks

# 시세+뉴스 수집 (전부 API 키 없이 yfinance로 바로 동작). job 하나가 실패해도
# 나머지는 계속 실행되고, 실행마다 collect_log에 성공/실패가 남는다.
python collectors/collect_daily.py         # us_market + kr_market + global_markets + news 한 번에
python collectors/collect_daily.py --period 6mo
python collectors/collect_daily.py --only kr,global,news   # 일부만 선택 실행 (디버깅·재시도용)
python collectors/collect_daily.py --dry-run               # 실제 수집 없이 실행될 job만 미리 보기

# 개별 실행하고 싶으면 (디버깅용, 위 collect_daily.py와 동일한 로직을 개별 호출)
python collectors/us_market.py 1y          # SOX·NVDA·MU·TSM·AMD·VIX
python collectors/kr_market.py 1y          # 000660·005930(KRX)·SKHY(ADR)·USDKRW
python collectors/kr_market.py max         # 2000년부터 전체 기간 (백테스트용으로 더 길게 필요할 때)
python collectors/global_markets.py 1y     # 코스닥·S&P500·다우 등 전문가 경험칙 신호용 주변부 지표
python collectors/news.py                  # 뉴스 헤드라인

# 데모: 수집한 데이터로 관계 측정
python scripts/demo.py

# 대시보드 (역시 API 키 없이 동작)
uvicorn web.main:app --reload
# http://127.0.0.1:8000 접속

# MCP 서버 (Claude Desktop / Claude Code에 연결)
python -m mcp_server.server
# 또는: claude mcp add baseline -- python -m mcp_server.server

# 전문가 발언/소문 기록 (claims 테이블. --affiliation/--stance는 선택)
python scripts/add_claim.py --who "..." --ticker 000660 --summary "..." --confidence news
python scripts/add_claim.py --who "..." --affiliation "어느증권" --stance 매수 \
  --ticker 000660 --summary "..." --confidence news --source-url https://...

# 기록된 발언 사후 채점 (목표가·기간 만기가 지난 건만, API 키 불필요)
python scripts/grade_claims.py --dry-run   # 미리보기, DB 안 건드림
python scripts/grade_claims.py

# 에이전트 배치 분석 (Anthropic API 호출 — 비용 발생. 먼저 --dry-run으로 전송 내용만 확인 가능)
python scripts/run_agents.py --ticker 000660 --dry-run
python scripts/run_agents.py --ticker 000660

# 기술적 지표 규칙 백테스트 (과거 데이터 측정만. 매매 실행·실시간 신호 없음)
# 데이터가 짧으면(1년 미만) 먼저 collectors/kr_market.py 5y(또는 max) 로 수집할 것을 안내한다
# 기본 시작일은 2005-01-01(000660의 2001년 전후 이상치 구간 회피). --start ''로 비우면 전체 기간.
python scripts/backtest_strategies.py                       # 기본 000660, SMA/RSI/볼린저/MACD+buy&hold
python scripts/backtest_strategies.py --ticker 005930 --threshold 0.75
python scripts/backtest_strategies.py --ticker 000660 --start '' --end 2024-12-31   # 2000년부터 전체

# 눌림목 지정가 매매 백테스트 (손절 있는/없는 버전 비교 + 장세별 성과·기대값/중앙값)
python scripts/backtest_strategies.py --strategy pullback
python scripts/backtest_strategies.py --ticker 005930 --strategy pullback --dip-pct 0.03 --stop-loss-pct 0.05

# 매수/목표/손절 기준가 규칙 백테스트 + 통과 판정(거래 100건↑·전체·최근1년 거래당평균 둘 다 양수)
python scripts/backtest_strategies.py --strategy price_rules
python scripts/backtest_strategies.py --ticker 005930 --strategy price_rules

# 전문가 경험칙 신호 (처음 설정할 때 순서대로)
python collectors/global_markets.py max           # 코스닥·S&P500 등 주변부 지표 (최초 1회는 전체 기간 권장)
python scripts/seed_signals.py                     # experts/signals 정의 등록(멱등)
python scripts/seed_analyst_views.py               # 2026-07~08 발언 기록 (1회만! 재실행하면 중복 저장됨)
python scripts/add_manual.py rate-stage --stage "2~3회" --effective-on 2026-09-01   # 수동 입력 예시
python scripts/add_manual.py pbr --value 1.05 --effective-on 2026-10-01
python scripts/compute_signals.py                  # signal_values 채움(멱등, 데이터 갱신마다 재실행)
python scripts/recompute_weights.py                 # 가중치 재계산(매주 1회 권장, 아래 스케줄 참고)
python scripts/recompute_weights.py --dry-run       # DB에 안 쓰고 미리보기만
python scripts/signal_scorecard.py                  # reports/signal_scorecard.md 생성

# 위 세 단계(수집 -> 신호 계산 -> 가중치 재계산)를 한 번에 체이닝 (매주 실행용)
python scripts/run_weekly.py                        # collect_daily(1y) -> compute_signals -> recompute_weights
python scripts/run_weekly.py --period 1y --min-events 20
python scripts/run_weekly.py --only kr,global,news   # collect_daily 단계에 --only를 그대로 전달
python scripts/run_weekly.py --dry-run               # 아무 것도 실행하지 않고 실행될 명령 3개만 미리 보기
# 세 단계는 서로의 입력에 의존하므로, collect_daily.py와 달리 한 단계가 실패하면
# (종료 코드 ≠0) 거기서 체인을 멈춘다 — 부실한 입력으로 다음 단계를 계속 돌리지 않음.

# 과거 위험 점수 재구성 (미래 참조 없이, 점수 구간별 이후 5/20/63일 수익률 분포)
python scripts/risk_score_history_report.py

# 데이터 정합성 확인 (6단계) — 자동 보정 없음, 비교/스캔 결과만 출력
python scripts/verify_outlier.py                    # 000660 2001-11-12 이상치, data/manual/의 공식 CSV와 대조
python scripts/verify_outlier.py --ticker 005930 --date 2020-03-19   # 임의 날짜도 대조 가능
python scripts/verify_outlier.py --scan-limits       # KRX 개별종목(000660·005930)을 날짜별 가격제한 테이블과 대조
python scripts/verify_outlier.py --scan-limits-all   # 위를 DB의 모든 티커로 확장(지수·선물 등 포함 — 오탐 주의)

# 테스트
pytest -q
```

### 전문가 경험칙 신호 가중치 주간 재계산 스케줄 등록

`scripts/run_weekly.py`(= collect_daily → compute_signals → recompute_weights)를
매주 일요일 자동 실행하도록 등록하는 예시다. **이 저장소/세션에서는 아래 등록을
실제로 수행하지 않았다** — 사용자가 본인 환경에서 직접 등록할 것.

**macOS/Linux (cron)**:
```bash
crontab -e
# 매주 일요일 09:00 (가상환경 경로를 실제 위치로 바꿀 것)
0 9 * * 0 cd /절대/경로/baseline && .venv/bin/python scripts/run_weekly.py >> logs/run_weekly.log 2>&1
```

**Windows (작업 스케줄러, 예시 — 실제 등록은 사용자가 직접 할 것)**:
1. `작업 스케줄러` 실행 → `기본 작업 만들기`
2. 트리거: `매주`, 요일 `일요일`, 원하는 시각
3. 동작: `프로그램 시작` — 프로그램/스크립트에 가상환경의 `python.exe` 전체 경로
   (예: `C:\절대\경로\baseline\.venv\Scripts\python.exe`), 인수 추가에
   `scripts\run_weekly.py` 하나만 넣으면 된다(이전엔 compute_signals.py와
   recompute_weights.py를 `&&`로 못 이어서 배치 파일이 필요했는데,
   `run_weekly.py`가 그 체이닝을 대신하므로 이제 배치 파일 없이 바로 가능):
   - 프로그램/스크립트: `C:\절대\경로\baseline\.venv\Scripts\python.exe`
   - 인수 추가: `scripts\run_weekly.py`
   - 시작 위치(옵션): `C:\절대\경로\baseline` (상대 경로 `db\`, `collectors\`
     등이 깨지지 않으려면 반드시 지정)
   - 배치 파일로 감싸고 싶다면 여전히 가능하다:
     ```bat
     @echo off
     cd C:\절대\경로\baseline
     .venv\Scripts\python.exe scripts\run_weekly.py
     ```

한국 **수급**(투자자별 매매동향) 데이터를 쓰려면 `.env.example`을 `.env`로 복사하고
KIS API 키를 넣는다 (모의투자 계좌로 발급할 것). **시세**(가격) 자체는 000660·SKHY 모두
yfinance로 받아오므로 KIS 키가 없어도 대시보드 전체가 동작한다.

**에이전트 배치 분석**(`scripts/run_agents.py`)을 쓰려면 `.env`에 `ANTHROPIC_API_KEY`를
추가해야 한다. 대시보드를 열거나 MCP 서버를 켜는 것만으로는 이 키가 없어도 전부 동작한다 —
API 호출은 `run_agents.py`를 사람이 직접 실행할 때만 발생하고, 결과는 DB에 캐싱돼서
대시보드/MCP는 캐시만 읽는다.

**DART(전자공시) 연동**을 쓰려면 `.env`에 `DART_API_KEY`를 추가한다
(https://opendart.fss.or.kr 무료 발급). 키가 없으면 `collectors/dart_client.py`가
네트워크를 전혀 시도하지 않고 조용히 건너뛴다 — 다른 모든 기능은 DART 키 없이도
그대로 동작한다(`YJH_PBR_FLOOR`/`YJH_MEMORY_MARGIN`도 기존처럼 수동입력으로 폴백).

```bash
# DART 연동 (DART_API_KEY 필요, 없으면 0건으로 조용히 끝남)
python collectors/collect_daily.py --only dart        # 000660·005930 재무제표+공시 수집(이미 있는 보고서는 재호출 안 함)
python collectors/dart_client.py                       # 위와 동일, 직접 실행

# DART 공시를 claims에 시딩 (confidence='official', 원문 링크 포함, 멱등)
python scripts/seed_disclosure_claims.py --stock-code 000660
python scripts/seed_disclosure_claims.py --dry-run     # 몇 건이 새로 기록될지만 미리보기

# 수집 후 신호 재계산(기존과 동일한 명령 — DART 데이터가 있으면 자동으로 우선 사용됨)
python scripts/compute_signals.py
```

### 하워드 막스 메모 수집 · 관점별 토론 (섀도 기록 — 매매 미반영)

```bash
# 메모 목록 수집(주 1회면 충분). 이용약관을 직접 읽고 config/marks_memos.yaml의 terms_reviewed를 true로 바꾸기 전에는
# 수집하지 않고 collect_log에 "수동 입력 필요"만 남긴다. robots.txt도 매번 확인한다.
python scripts/run_weekly.py --only memos          # 또는 python collectors/marks_memos.py
python collectors/marks_memos.py --manual --title "제목" --url https://... --date 2026-10-01 --summary "내가 쓴 요약"

# 관점별 토론 — 기본은 dry-run(프롬프트·예상 토큰·최악 비용만 출력, API 호출 없음)
python scripts/run_debate.py
python scripts/run_debate.py --show-prompts
python scripts/run_debate.py --execute             # 실제 Anthropic API 호출(비용 발생). 1회 토큰·월 예산 상한은 config/debate.yaml

# 기록이 쌓인 뒤 채점(결론별 이후 5/20/63거래일 수익률, 같은 날 위험 점수 상태와의 일치율, 표본 60 미만이면 판단 불가)
python scripts/debate_scorecard.py
```

- **메모 요약은 개인 참고용이며 외부에 공개하지 않는다.** 메모 본문 전체는 DB에 저장하지 않는다(제목·게시일·URL·본문 해시만).
  요약은 토론 실행 때 필요하면 그때 본문을 받아 짧게 만들고 본문은 버린다. 원문 40자 이상 같은 구간이 있으면 요약을 버린다.
- 토론 관점은 사이클·금리·유동성·가치·추세·반대이고 **실존 인물 이름으로 1인칭 발언을 만들지 않는다.** 결론은
  "위험 높음/중립/위험 낮음"과 근거 데이터 키뿐이다. 예측·매매 지시·인물 인용이 감지되면 저장하지 않고 검토 대기로 보낸다.
- **토론 결과는 기록만 한다.** 위험 점수·상태 문구·executor 어디에도 반영하지 않는다(정적 검사 테스트로 고정).
- **과거 날짜로 토론을 돌리는 기능은 없다.** 언어 모델은 과거 시장에서 실제로 일어난 일을 학습했을 수 있어서, 과거 날짜로
  돌리는 백테스트는 미래참조로 오염된다. 그래서 앞으로 쌓이는 기록만 채점한다.
- **자동 스케줄(run_weekly·작업 스케줄러)에 넣지 않는다 — 수동 실행만.** 돌릴 때마다 API 비용이 나가기 때문이다.

## 알려진 제약

- **ADR 전환비율(1:10)**: 공개된 3차 자료(추적 사이트) 기준으로 코드에 넣었다.
  공식 예탁계약서로 재확인 전까지는 `analysis/adr.py`의 `DEFAULT_RATIO` 하나만
  보면 되게 만들어뒀다. 대시보드 "ADR 프리미엄 상세" 카드에도 미확인 상태라는
  안내가 같이 뜬다. 값이 다르다고 확인되면 `.env`의 `ADR_RATIO`로 override
  (코드 수정 불필요).
- **가격제한(상하한) 테이블(`analysis/price_limits.py`)**: 2015-06-15 ±30%
  개편 하나만 넣었다(비교적 널리 알려진 날짜). 그 이전 연혁(6%/8%/12%/15%
  단계)은 정확한 시행일을 확인 못해 비워뒀다 — `scripts/verify_outlier.py
  --scan-limits`가 2015-06-15 이전 날짜는 "판정 불가"로 건너뛴다(위반도
  정상도 아니라고 함부로 말하지 않음).
- **DART(전자공시) 연동(`collectors/dart_client.py`)**: 이 세션은 DART 키
  없이 구현했다 — 엔드포인트 경로·tr 파라미터·응답 필드명(`account_nm`·
  `thstrm_amount`·`rcept_dt` 등)은 공식 문서 기준 best-effort로 작성했지만
  실제 호출로 검증한 적이 없다(코드 상단 TODO 주석 전부). **특히
  발행주식수(`stock_total_count`) 쪽은 신뢰도가 가장 낮다** — 필드명 후보
  여러 개를 순서대로 시도하고 전부 실패하면 "확인 불가"로 PBR 계산을
  건너뛴다. 키 발급 후 가장 먼저 raw 응답을 한 번 확인할 것(아래 "DART 키
  받은 뒤 확인할 것" 체크리스트 참고— CLAUDE.md 세션 로그에 남겨둠).
- **투자 프레임워크 렌즈**: 실존 인물의 1인칭 발언을 만들지 않는다. 프레임워크
  이름만 참고하고, 재무데이터(PER·PBR·EPS 등)가 필요한 가치·성장·밸류에이션
  렌즈는 데이터 소스가 없어 "확인 필요"로 정직하게 비워뒀다.
- **시장 심리 게이지**: CNN Fear & Greed Index의 복제가 아니다. CBOE VIX
  하나로 방법론을 공개한 자체 대체 지표다 (`analysis/sentiment.py` 참고).
- **뉴스**: 본문을 저장하지 않는다 (저작권). 제목·출처·날짜만 저장하고,
  출처가 불확실한 항목(`confidence='rumor'`)은 화면에 항상 "미확인" 표시가 붙는다.
- **에이전트 리서치 요약**: 실시간이 아니라 배치 작업(`scripts/run_agents.py`) 시점에
  캐싱된 결과다. 시스템 프롬프트로 방향 예측·매매 조언·실존 인물 인용을 금지하지만,
  LLM 출력이라 완벽을 보장할 수 없다 — 응답 텍스트에서 금지 문구를 다시 훑는 2차
  검사(`agents/orchestrator.py`의 `_scan_for_violations`)를 통과하지 못하면 DB에
  저장하지 않고 사람이 직접 검토하게 만들어뒀다.
- **전문가 발언 채점**(`scripts/grade_claims.py`): 목표가(`target_price`)·기간
  (`horizon_days`)이 둘 다 있는 발언만 채점 대상이다. 목표가가 발언 시점 가격과
  같아 방향이 불명확한 경우는 miss(0)로 처리한다(방향을 안 밝힌 발언을 관대하게
  적중 처리하면 채점 기준이 무의미해진다고 판단). 만기 시점 이후 시세가 아직
  수집되지 않은 건은 조용히 건너뛴다 — 틀린 값으로 채점하는 것보다 안전하다.
- **애널리스트/의견 추적**(대시보드 "애널리스트/의견 추적" 카드): `claims`
  테이블을 그대로 재사용한다(`affiliation`·`stance` 컬럼만 추가) — 완전히
  새로운 테이블이 아니다. 사람(`who`)당 가장 최근 의견 1건만 보여주고, 그
  이후 실제 주가 변화는 `price_then`(기록 시점 종가) 대비 **현재 최신
  종가**로 계산한다(채점 완료·만기 도래 여부와 무관하게 바로 보여줄 수
  있음 — `scripts/grade_claims.py`의 사후 채점과는 별개). `summary`는
  여전히 원문 인용이 아니라 본인이 요약한 한 줄이어야 한다(원칙 5) —
  `--affiliation`·`--stance`는 분류 라벨일 뿐 실존 인물의 발언을 지어내는
  걸 허용하지 않는다.
- **mcp SDK 버전**: `mcp>=2.0`은 `mcp.server.fastmcp.FastMCP`가 없어져서
  `mcp_server/server.py`와 호환되지 않는다. `requirements.txt`에
  `mcp>=1.2,<2.0`으로 핀해뒀다 — `pip install`할 때 이 범위를 벗어나지
  않도록 주의할 것.
- **기술적 지표 규칙 백테스트**: 이번 단계는 과거 데이터 측정만이다. 주문/매매
  실행 코드는 없고(CLAUDE.md 원칙 2), "오늘의 신호" 같은 실시간 출력도 없다.
  거래비용(수수료·세금)은 참고용 근사치이며 실제 요율은 증권사·연도별로
  다르다. 파라미터(이동평균 기간 등)는 전부 기본값만 쓴다 — 적은 데이터로
  파라미터를 데이터에 맞춰 고르면 과거에만 맞는 우연한 조합을 "전략"으로
  착각하기 쉽다(과최적화). 같은 이유로 기간을 앞 70%(in-sample)/뒤
  30%(out-of-sample)로 나눠 따로 지표를 낸다. 대시보드·MCP 툴
  (`get_rule_backtest`)도 이 결과를 노출하지만, 대시보드 카드는 요약용으로
  **전체 기간만** 보여준다 — in-sample/out-of-sample 분리와 threshold 비교는
  `scripts/backtest_strategies.py` CLI에서 확인할 것.
- **눌림목 지정가 매매 백테스트**(`analysis/pullback.py`,
  `scripts/backtest_strategies.py --strategy pullback`): 일봉(시가/고가/저가/
  종가)만으로 체결·손절·익절을 판정하다 보니 **같은 날 손절가와 익절가가
  둘 다 닿으면 손절이 먼저 체결된 것으로 본다** — 실제 장중에 어느 쪽이
  먼저인지는 일봉만으로 알 수 없어서, 결과를 유리하게 부풀리지 않기 위한
  보수적 가정이다. 같은 이유로 진입 당일은 그날 바로 손절/익절 체크를 하지
  않는다(다음날부터 체크, 하루 단위 단순화). 장세별 buy&hold 비교 수치는
  "그 장세로 분류된 날짜만 띄엄띄엄 보유했다면"을 가정한 이상화된 수치라
  실제로 그렇게 거래할 수 있다는 뜻은 아니다 — 기회비용 크기를 보여주기
  위한 비교 기준일 뿐이다. 이 전략의 기본 파라미터(dip 2%/익절 2%/손절
  3%/최대보유 10일)는 전혀 최적화되지 않았고, 과최적화 방지 목적으로
  의도적으로 기본값만 썼다 — 실전에 맞는지는 검증된 바 없다. **장세별
  buy&hold는 t일까지의 데이터로 분류한 날의 t→t+1일 수익률만 집계한다**
  (그날 자신을 분류하게 만든 수익률을 되돌려 집계하는 순환 참조를 피하려는
  것 — 분류와 측정 구간이 겹치지 않는지 직접 재현 테스트로 확인했다).
  거래당 평균손익(기대값)·중앙값·최악거래는 buy&hold와는 무관하게 **규칙
  자체의 거래 성적**만 보는 지표다.
- **매수/목표/손절 기준가**(`analysis/price_rules.py`,
  `analysis/price_rule_backtest.py`, `scripts/backtest_strategies.py
  --strategy price_rules`, 대시보드 "매수·목표·손절 기준가" 카드):
  CLAUDE.md 원칙 1의 유일한 예외다 — 백테스트 통과 기준(거래 100건 이상,
  전체 기간·최근 1년 거래당 평균수익 둘 다 양수)을 만족한 규칙만 가격을
  보여준다. 손절·익절 판정은 `analysis/pullback.py`와 같은 보수적 가정
  (같은 날 둘 다 닿으면 손절 우선, 진입 당일은 체크 안 함)을 쓴다.
  실제로 000660 기준 3개 규칙 중 "이동평균 눌림목" 하나만 통과했고
  "ATR 밴드"·"전일 고저 돌파"는 거래당 평균이 음수라 탈락했다 — 규칙
  이름만으로 다 통과하지 않는다는 뜻이다. 과거에 통과했다고 미래에도
  같은 성과를 보장하지 않으며, 표시되는 가격으로 주문이 자동 실행되는
  경로는 없다.
- **000660 과거 데이터 이상치**: `period=max`로 받은 2000년 이후 데이터
  중 **2001-11-12에 +32.08%** 단일 거래일 변동이 있다 — 당시 KRX 일일
  가격제한(역사적으로 ±15% 수준)을 넘는 값이라 정상 거래로 보기 어렵고,
  하이닉스의 2001년 전후 재무위기·구조조정(2003년 약 1:21 무상감자 포함)
  시기와 겹쳐 데이터 보정이나 거래정지 재개 흔적으로 추정된다(원인 확정은
  못 했다). `scripts/backtest_strategies.py`의 `--start` 기본값을
  `2005-01-01`로 둬서 이 구간을 기본적으로 피한다 — `--start ''`로 비우면
  전체 기간을 그대로 쓸 수 있다. 005930(삼성전자)은 같은 2000~2006년
  구간에 ±30% 초과 이상치가 없다.
- **전문가 경험칙 신호**(`analysis/signals.py` 등): 신호는 전문가가 영상에서
  말한 경험칙을 사용자가 전달한 요지 그대로 기계적으로 계산한 것이며, 실존
  인물의 실시간 판단이나 1인칭 발언을 재현한 게 아니다(원칙 5). 알려진
  데이터/해석 제약:
  - `KSJ_FOREIGN_SELL`은 KIS `investor_flow()`가 미구현이라 아직 계산되지
    않는다(`enabled=0`) — 구현 후 연결할 것, 가중치 상한 15%는 이미 설정됨.
  - `KSJ_BREADTH`("반도체 쏠림")는 KOSPI 상승종목비율 데이터를 구할 수 없어
    반도체 2종목 가중평균 수익률로 근사했다 — 실제 상승종목비율과 다를 수
    있다(확인 필요).
  - 베트남 지수는 yfinance에 없어 VanEck Vietnam ETF(`VNM`)로 대리했다 —
    지수와 완전히 같지 않다.
  - `KHJ_RATE_STAGE`·`YJH_MEMORY_MARGIN`·`YJH_PBR_FLOOR`는 수동 입력
    (`scripts/add_manual.py`) 의존이라, 값을 한 번도 입력하지 않으면 계산되지
    않는다(지어내지 않음 — "확인 필요"로 조용히 건너뜀).
  - `KSJ_DCA`(분할 매수 포맷)는 on/off 신호가 아니라 표시 방식 규칙이라
    백테스트·가중치 대상이 아니다.
  - 가중치 상한(신호 30%·전문가 40%)·위험 점수 전환 기준(60/80점)은 사용자가
    지정한 시작 기본값이다 — 백테스트 결과에 맞춰 거꾸로 고른 게 아니다
    (과최적화 방지).
  - `analysis/signal_backtest.py`의 "최대낙폭"은 여러 신호-on일의 N거래일
    뒤 수익률 분포에서 가장 나빴던 단일 사례를 뜻한다 — 하나로 이어지는
    자본 곡선의 낙폭이 아니다(온-일들이 겹쳐서 그런 곡선을 만들 수 없음).
  - `reports/signal_scorecard.md`는 거래비용(수수료·세금)을 반영하지
    않는다 — 매매 시뮬레이션이 아니라 "신호 이후 가격 변화" 관측이라 체결할
    거래 자체가 없다.

## 면책

- 이 저장소는 **개인 학습·포트폴리오용** 프로젝트다.
- 여기 있는 어떤 숫자·표·상태 문구도 **투자 권유가 아니다.** 특정 종목·시장·전략을 사거나 팔라는 뜻이 없고, 어떤 수익도 보장하지 않는다.
- 이 코드를 사용하거나 수정해 생긴 **어떤 손실에도 작성자는 책임지지 않는다.**
- 전문가 경험칙은 공개 방송에서 들은 요지를 직접 정리한 것이며(원문 인용 아님) 발언자를 대변하지 않는다. 공개 파일에는 실명 대신 "전문가 A/B/C"로 적는다.

개인 학습용 프로젝트다. 투자 권유가 아니며 어떤 수익도 보장하지 않는다.
확률이나 narrative 형태의 예측 기능은 의도적으로 넣지 않았다 — 방향을 확신
있게 말하든 확률로 완곡하게 말하든, 미래를 전망한다는 본질은 같다고 봤기
때문이다. 유일한 예외는 매수/목표/손절 "기준가"인데, 이것도 전망이 아니라
엄격한 백테스트 기준(거래 100건 이상, 전체 기간과 최근 1년 모두 거래당 평균
수익이 양수)을 통과한 규칙을 오늘 그대로 적용했을 때 나오는 기계적 계산값일
뿐이다 — 과거에 통과했다고 미래에도 같은 성과를 보장하지 않으며, 주문을
자동으로 실행하는 경로도 없다.
