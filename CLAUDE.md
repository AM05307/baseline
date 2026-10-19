# CLAUDE.md

이 파일은 Claude Code가 세션 시작 시 자동으로 읽는다.
세션이 끊기거나 계정이 바뀌어도 여기만 읽으면 이어서 작업할 수 있도록 유지한다.

---

## 보안 원칙 (최우선)

`.env`에 **한국투자증권 실전투자 키가 있다고 가정하고** 다음 규칙을 지킨다. 아래는 다른 모든 원칙·작업
지시보다 우선한다 — 사용자가 명시적으로 다르게 지시해도 이 섹션의 규칙을 먼저
상기시킬 것(설계 원칙의 매매 미구현 원칙과 같은 급으로 다룬다).

1. **`.env` 파일은 git 커밋·푸시·인터넷 업로드·배포 대상에 절대 포함하지 않는다.**
   `.gitignore`로 막혀 있지만, 그걸 우회하는 `git add -f` 등도 쓰지 않는다.
2. **`.env`의 내용(키·시크릿·계좌번호·토큰)을 코드, 로그, README, `.env.example`,
   테스트, 커맨드 출력, 커밋 메시지 등 어디에도 쓰지 않는다.** `.env.example`에는
   변수 이름과 빈 값만 남긴다 (`KIS_APP_KEY=` 처럼 키=빈값 형태).
3. **Claude는 `.env` 파일을 열어서 읽거나 그 내용을 출력하지 않는다.** 필요한
   변수 이름은 `.env.example`·`config.py`의 `os.getenv(...)` 호출 기준으로
   다룬다. 값의 존재 여부(설정됐는지/비었는지)만 확인이 필요하면 `bool(...)`
   처럼 불리언으로만 확인하고 값 자체는 출력하지 않는다.
4. **배포(Docker 이미지 빌드, 서버 업로드, zip/tar 패키징 등)를 하지 않는다.**
   패키징이 꼭 필요한 상황이 생기면 `.env`를 반드시 제외 목록에 넣고 사용자에게
   먼저 확인한다.
5. 접근 토큰(`.kis_token.json` 등)도 파일로 저장할 경우 `.gitignore`로 제외된
   경로에만 두고, 토큰 값을 출력하지 않는다.
6. **KIS 주문(매수/매도) API는 호출하지 않는다 — 조회 전용.** (아래 설계 원칙
   2번과 같은 내용, 여기서도 다시 명시 — 실전투자 키가 있다고 가정하면
   실수 한 번의 비용이 매우 크다.)

---

## 프로젝트 개요

**baseline** — 주식 시장 데이터를 수집하고 관계를 **측정**하는 개인 대시보드.

이름의 뜻: 감이나 전문가 말이 아니라 **데이터에서 나온 기준선**으로 판단하기 위한 도구.
개인 학습용이며 투자 권유 목적이 아니다.

파이썬은 이 프로젝트로 익히는 중. 이론 학습보다 **기능을 만들면서 배우는 방식**을 선호한다.

---

## 설계 원칙 (가장 중요 — 절대 어기지 말 것)

이 프로젝트에는 명시적으로 하지 않기로 한 것들이 있다.
편의를 위해 어기지 말 것. 어길 거면 사용자에게 먼저 확인할 것.

### 1. 백테스트로 검증된 규칙만 매수/매도 기준가로 표시한다 (2026-10-06 갱신)
- (이전 버전: "주가를 예측하지 않는다" — 전망·확률·목표가 전부 금지. 아래로
  대체됐지만 **LLM이 만드는 서술에는 여전히 100% 그대로 적용**된다.)
- 가격을 "예측"하지 않는다는 본질은 그대로다 — 다만 **기계적으로 계산되고
  엄격하게 검증된** 매수/목표/손절 기준가는 화면에 보여줄 수 있다.
- "검증됨"의 기준(`analysis/price_rule_backtest.passes()`, 사용자가 명시한
  기준 그대로):
  1. 거래 수 100건 이상(전체 기간, 거래비용 반영 후)
  2. 전체 기간 거래당 평균수익이 양수
  3. 최근 1년(out-of-sample, 검증 구간)의 거래당 평균수익도 양수
  - 세 조건 중 하나라도 거짓이면 그 규칙은 **화면에 가격을 절대 노출하지
    않는다**. 통과한 규칙이 하나도 없으면 "오늘 해당 규칙 없음"이라고만
    표시한다 (`web/main.py`의 "매수·목표·손절 기준가" 카드,
    `scripts/backtest_strategies.py --strategy price_rules`).
  - 표시되는 가격은 "이 규칙을 오늘 그대로 적용하면 나오는 레벨"일 뿐이고,
    향후 가격이 그렇게 될 거라는 전망이 아니다. 과거 성적(n·승률·거래당
    평균·MDD)을 항상 같은 화면에 같은 비중으로 같이 보여준다(원칙 3·4).
- **이 예외는 LLM이 만드는 서술에는 적용되지 않는다.** 에이전트 리서치 요약
  (`agents/orchestrator.py`)·투자 프레임워크 렌즈·MCP 서술형 응답은 여전히
  "오를 것이다"류의 방향성 판단이나 목표가·확률 narrative를 만들어내면
  안 된다 — 기계적으로 계산되고 백테스트를 통과한 숫자와, LLM이 그때그때
  지어내는 서술은 완전히 다른 신뢰 수준이기 때문이다. `_scan_for_violations()`
  와 금지 문구 목록은 그대로 유지한다.
- 상관계수·베타·승률 자체를 근거로 방향을 단정하는 것도 여전히 금지다.
- **위험 점수 상태 문구("매수 가능"/"보류"/"분할 매도 N/3 단계")도 같은
  예외에 속한다(2026-10-06 세션 4차, 5단계에서 사용자가 "CLAUDE.md 원칙을
  갱신" 쪽으로 확정).** `analysis/group_score`가 켜진 신호를 미리 정한
  정보 출처 그룹으로 묶어(그룹 안·그룹끼리 균등 가중치) 0~100 위험 점수로
  기계적으로 환산하고, 기준 기간(2005-01-01~2018-12-31) 위험 점수 분포의
  상위 30%/15%/5%(목표 백분위. 점수 동점 때문에 기준 기간의 실제 비율은
  24.4/7.8/3.2%)로 정해 고정한 단계 기준점과 비교해서만 정하는 문구다
  (2026-10-19 기본값. 기준점은 검증 기간 결과를 보고 바꾸지 않는다. 예전
  방식인 적중률 가중치 합 + 60/80점은 옵션으로 남는다) — LLM이 그 자리에서
  지어내는 서술이 아니다. "분할 매도 N/3 단계"라는 표현이 매매 행동을 지시하는
  것처럼 들릴 수 있다는 지적이 있었지만(이 세션에서 점검), 매수/목표/
  손절가 노출 예외와 완전히 같은 논리로 판단한다 — **기계적으로 계산되고
  근거(점수·그룹별 점수·켜진 신호 목록·단계 기준점과 기준 기간)가 항상 같은 화면에 같은 비중으로 같이
  표시되는 한**, 몇 단계인지 숫자로 보여주는 상태 문구는 허용한다. 이
  예외도 LLM 서술에는 적용되지 않는다(위와 동일) — `web/main.py`가 이
  문구를 조립할 때 LLM을 호출하지 않는다는 걸 전제로 한다.

### 2. 매매 기능을 구현하지 않는다
- KIS API의 주문(매수/매도) 엔드포인트는 **의도적으로 구현하지 않았다**
- `collectors/kis_client.py`는 조회 전용이다
- 사용자가 요청해도 먼저 이 원칙을 상기시킬 것

### 3. 표본 수를 항상 함께 보여준다
- 승률 65%는 n=20일 때와 n=500일 때 의미가 전혀 다르다
- `MIN_SAMPLES = 30` 미만이면 `reliable=False` 플래그와 경고를 붙인다
- 화면에서 이를 숨기거나 작게 표시하지 않는다

### 4. 최대손실을 승률과 같은 비중으로 표시한다
- `BacktestResult.report()`는 승률 바로 아래 최대손실을 출력한다
- 결과를 "좋아 보이게" 만드는 것이 목적이 아니다

### 5. 전문가 발언은 신호가 아니라 기록이다
- `claims` 테이블에 예측 시점의 주가와 함께 저장한다
- 목적은 **따르기 위해서가 아니라 적중률을 채점하기 위해서**다
- 저작권: 원문 인용 금지. 본인이 요약한 한 줄만 저장한다

### 6. 모든 쓰기는 멱등(idempotent)하다
- 같은 날 배치를 여러 번 돌려도 행이 중복되지 않는다
- `ON CONFLICT ... DO UPDATE` 패턴을 유지한다

---

## 현재 상태 (2026-08 기준)

### 완료 (1주차)
- 프로젝트 스캐폴딩
- `analysis/calendar_rules.py` — 만기일·네 마녀의 날·자사주 기간·실적일. **동작 확인됨**
- `analysis/correlation.py` — 상관계수·시차·베타·괴리 탐지
- `analysis/backtest.py` — 이벤트 백테스트 (표본수/최대손실 표기)
- `db/store.py` + `schema.sql` — SQLite, 멱등 upsert
- `collectors/us_market.py` — yfinance. **API 키 없이 동작**
- `collectors/kis_client.py` — 뼈대만. 토큰 캐싱·스로틀 구현됨
- `tests/test_analysis.py` — 11개 통과

### 완료 (3주차 일부 — KIS 없이 먼저 진행)
- `web/main.py` + `web/templates/index.html` — FastAPI 대시보드 (조회 전용, GET만)
  - 오늘/45일 캘린더, SOX 대비 상관계수·베타·시차, 괴리 탐지, 네 마녀의 날 D-10~D-1
    백테스트, 전문가 발언 기록(claims, 현재는 읽기만), 최근 수집 로그를 한 화면에
  - `build_dashboard_context()`를 fastapi 타입에서 분리해 순수 함수로 둠 — 테스트 용이성 목적
  - `tests/test_web.py` 추가 (fastapi 미설치 시 자동 skip)
  - **아직 안 한 것**: 실행 확인은 이 세션에서 fastapi 설치가 막혀 있어 못 했음.
    로컬에서 `pip install -r requirements.txt` 후 `uvicorn web.main:app --reload`로 직접 확인할 것.
    로직 자체는 jinja2로 렌더링까지 시뮬레이션해서 검증함(합성 데이터, 예측성 문구 없음 확인).
  - `WATCH_US`에 KIS 데이터(하이닉스)가 없어서 지금은 미국 종목끼리의 관계만 보여줌.
    KIS 붙으면 `collectors/collect_daily.py`가 채워주는 데이터를 그대로 읽어서 확장 가능.

### 완료 (5주차 일부 — KIS 없이 먼저 진행)
- `mcp_server/tools.py` — 조회 전용 로직 5개 (`get_correlation`, `get_divergence_days`,
  `get_calendar_events`, `get_backtest`, `get_collection_status`). mcp 패키지에 의존하지
  않는 순수 함수라 **이 세션에서 실제로 실행해 검증함** (mcp SDK 없이도 동작 확인 가능했음).
- `mcp_server/server.py` — FastMCP로 위 5개를 툴로 등록하는 얇은 배선. server instructions에
  "이 값 근거로 방향 단정 금지 / n<30이면 반드시 알릴 것 / 매매 기능 없음" 명시.
- `tests/test_mcp_server.py` 추가.
- **부수적으로 발견한 버그 고침**: `db/store.py`의 모든 함수가 `path: Path = DB_PATH`처럼
  기본값을 정의 시점에 고정해서, 테스트에서 `store.DB_PATH`를 바꿔치기해도 반영이 안 되는
  문제가 있었음. `path: Path | None = None` + 함수 내부에서 `path or DB_PATH`로 고쳐서
  런타임에 현재 `DB_PATH`를 읽도록 수정. 동작은 그대로, 테스트 용이성만 개선됨.
- **아직 안 한 것**: `mcp.server.fastmcp`도 이 샌드박스에서 설치가 막혀서 `server.py` 자체의
  실행은 못 해봄. 로컬에서 `pip install -r requirements.txt` 후
  `python -m mcp_server.server`로 기동 확인하고, Claude Desktop/Code에 붙여서 실제 툴
  호출까지 확인할 것.
- `analysis/calendar_rules.py`에 `historical_quad_witching()` 추가 — web 대시보드와
  MCP 서버가 과거 이벤트 백테스트에 공통으로 씀 (중복 제거).

### 완료 (2026-08-23 세션 — 대시보드 대폭 확장, 여전히 KIS 없이)

사용자가 대화 중 대시보드에 여러 기능(하이닉스 ADR·반도체 관련주 차트, 시장 심리
게이지, 뉴스, 유명 투자자 에이전트, 확률 기반 예측)을 요청. 원칙과 충돌하는 요청은
**진행하지 않고 사용자에게 이유를 설명**했다 — 아래 "이번 세션에서 조율한 원칙 충돌"
참고. 나머지는 실제로 구현함.

- **KIS 없이 하이닉스 시세를 구할 방법을 찾음**: `000660.KS`(KRX)와 `SKHY`(SK hynix
  ADR, 2026년 나스닥 상장)가 둘 다 yfinance로 받아진다. `collectors/kr_market.py` 신규
  추가 — `000660`·`SKHY`·`USDKRW`(환율) 수집. **KIS는 이제 수급(투자자별 매매동향)
  전용으로만 필요**하다.
- `analysis/adr.py` 신규 — ADR-KRX 프리미엄 계산. 전환비율 **1 ADS = 보통주 1/10주**
  (출처: skhypremium.com의 계산식 설명, 공식 예탁계약서로 재확인 전까지 코드 한 곳
  (`DEFAULT_RATIO`)만 보면 되게 함). 패리티가격 = KRX종가/환율/10.
- `analysis/sentiment.py` 신규 — VIX 기반 시장 심리 게이지. **CNN Fear & Greed
  Index는 무료 API가 없어서 그대로 못 가져옴** — 대신 VIX 백분위(최근 252거래일)를
  뒤집은 자체 게이지로 대체. 방법론을 코드 주석에 전부 공개.
- `collectors/news.py` 신규 — yfinance `Ticker.news`로 헤드라인만 수집(본문 저장
  안 함, 저작권). `db/schema.sql`에 `news_items` 테이블 추가.
- `claims` 테이블에 `confidence`(`news`|`rumor`) 컬럼 추가 + `db/store._migrate()`로
  기존 DB에도 안전하게 컬럼 추가 (멱등, 원칙 6). `scripts/add_claim.py` 신규 —
  전문가 발언/소문 기록 CLI (이전 세션부터 미완이던 항목).
- `analysis/frameworks.py` 신규 — "투자 프레임워크 렌즈". 리스크·사이클·팩트체커
  3개는 **실제 계산 가능**해서 구현. 가치·성장·밸류에이션은 재무데이터(PER/PBR/EPS)가
  없어서 **정직하게 "확인 필요" 스텁**으로 남김 — 그럴듯하게 지어내지 않음.
- `web/charts.py` 신규 — 서버사이드 SVG 차트(그리드라인·축 라벨 포함). JS 차트
  라이브러리 의존성 추가 안 함.
- `web/main.py` + `web/templates/index.html` **전면 재작성** — 가로형 와이드 레이아웃,
  흰 배경 모노크롬 + 포인트컬러 1개(파랑), Apple Stocks 앱 느낌. KPI 리본 · 000660
  vs SKHY 정규화 차트 · 반도체 관련주 미니차트 5개 · 상관관계 테이블 · 뉴스 패널 ·
  프레임워크 렌즈 패널 · VIX 게이지 · ADR 프리미엄 · 백테스트 · 캘린더로 구성.
- **버그 하나 고침**: `DataFrame.to_dict("records")`가 SQL NULL을 NaN으로 넘겨서
  템플릿의 `{{ x or "기본값" }}` 패턴이 안 먹히고 화면에 문자 그대로 "nan"이 찍히는
  버그를 실제로 재현하고 고침 (`web/main.py`의 `_records()` 헬퍼, `mcp_server/tools.py`
  에도 동일 헬퍼 추가). 뉴스 패널에서 출처 없는 항목을 테스트하다가 발견함.
- `mcp_server/tools.py`에 툴 4개 추가: `get_adr_premium`, `get_market_sentiment`,
  `get_news`, `get_framework_check`. 총 9개 툴.
- **검증 방법**: 이 샌드박스는 pypi 접근이 막혀서 fastapi/mcp를 설치할 수 없었다.
  대신 `fastapi`/`fastapi.responses`/`fastapi.templating`/`pytest`의 최소 기능만
  흉내내는 스텁을 `sys.modules`에 등록하고 **`web/main.py`·`mcp_server/tools.py`를
  수정 없이 그대로 import해서 실행**하는 방식으로 검증했다 (로직을 베껴서 별도
  테스트하지 않음). 합성 데이터로 빈 DB/채워진 DB 양쪽 다 렌더링까지 확인,
  예측성 문구·"nan" 문자열 없는지 자동 검사, 스크린샷 육안 확인까지 마침.
  그래도 **로컬에서 진짜 `pip install -r requirements.txt` 후
  `uvicorn web.main:app --reload`로 한 번은 직접 열어볼 것** — 특히 yfinance가
  실제로 000660.KS/SKHY/KRW=X를 문제없이 반환하는지는 네트워크가 되는 환경에서만
  확인 가능하다.

### 완료 (2026-08-23 세션 2차 — MCP를 실제 LLM 에이전트로 연결)

같은 날 사용자가 "앞으로 더 개발할거있어?"라고 물어서 남은 로드맵을 제시했고,
사용자가 "MCP 서버를 실제 LLM 에이전트로 연결"을 선택. 이어서 두 가지를 물어서
확정: (1) 실행 방식 — **배치로 미리 돌려 DB에 캐싱** (대시보드 로딩마다 API를
부르지 않음), (2) API 접근 방식 — **Anthropic API 키를 `.env`에 넣어서 직접 호출**.
사용자가 곧이어 "코드 구현할 수 있는 건 구현해줘, 업로드하지 말고 결제하지 말고"라고
못 박아서, 이 세션에서는 절대 실제 API를 호출하지 않고(비용 없음) 로직만 가짜
클라이언트로 검증했다.

- `agents/orchestrator.py` 신규 — **anthropic 패키지를 import하지 않는다.**
  `analyze_ticker(client, ticker, model, ...)`가 받는 `client`는
  `.messages.create(...)`만 있으면 되는 덕 타이핑 객체다. 실제 운영에서는
  `anthropic.Anthropic()`을 넘기고, 테스트에서는 미리 정해둔 응답을 순서대로
  돌려주는 가짜 클라이언트를 넘긴다 — 그래서 이 파일의 tool-use 루프 로직은
  anthropic 패키지 설치 여부와 무관하게 검증 가능하다(이 세션에서도 그렇게
  검증함, 아래 "검증 방법" 참고).
  - `mcp_server/tools.py`의 함수 8개(`get_correlation`·`get_divergence_days`·
    `get_calendar_events`·`get_backtest`·`get_adr_premium`·`get_market_sentiment`·
    `get_news`·`get_framework_check`)를 Anthropic tool-use 스펙(`TOOL_SPECS`)으로
    감싸서 그대로 에이전트에 노출. MCP 와이어 프로토콜(stdio)을 다시 구현하지
    않고 같은 파이썬 함수를 직접 호출한다 — 이건 프로젝트 내부 배치 스크립트라
    외부 MCP 클라이언트가 필요로 하는 프로토콜 배선은 필요 없다고 판단.
  - **가드레일 이중화**: 시스템 프롬프트에 원칙 1(예측 금지)·2(매매 금지)·
    3(표본수 고지)·5(실존 인물 1인칭 발언 금지, 프레임워크 이름만 허용)를
    그대로 명시했고, **거기서 끝내지 않고** 응답 텍스트를 금지 문구 목록으로
    다시 훑는 `_scan_for_violations()`를 2차 방어선으로 추가했다(defense in
    depth — 프롬프트만으로 모델이 안 지킬 가능성을 가정). 위반이 걸리면
    `AgentAnalysis.warnings`에 담기고, `scripts/run_agents.py`는 이 경우
    DB에 저장하지 않고 사람이 직접 검토하게 한다(아래).
  - 툴을 한 번도 안 부르고 답하면 "근거 데이터 없이 작성됐을 가능성" 경고를
    자동으로 붙인다 — 시스템 프롬프트가 `get_framework_check` 최소 1회 호출을
    요구하지만 이것도 강제는 아니라서 별도 점검.
  - `max_tool_iterations`(기본 6) 상한으로 무한 루프·과도한 API 호출을 막는다.
- `db/schema.sql` — `agent_analyses` 테이블 신규. `(ticker, run_date)` 유니크로
  멱등 처리(원칙 6) — 같은 날 배치를 다시 돌려도 새 행이 아니라 갱신됨.
  `tool_calls` 컬럼에 에이전트가 실제로 호출한 툴과 인자·반환값을 JSON으로
  통째로 남겨서 나중에 "무슨 데이터를 보고 이렇게 썼는지" 감사 가능하게 함.
- `db/store.py` — `upsert_agent_analysis()`, `get_latest_agent_analysis()` 추가.
- `scripts/run_agents.py` 신규 — **이 프로젝트에서 Anthropic API를 실제로
  호출하는 유일한 지점.** `--dry-run` 플래그로 실제 호출 전에 시스템/유저
  프롬프트·툴 목록을 먼저 눈으로 확인할 수 있게 했다(비용 없음). API 키가
  `.env`에 없으면 네트워크 시도 자체를 안 하고 바로 안내 메시지 출력. 가드레일
  위반이 감지되면 저장하지 않고 exit code 2로 종료 + 생성된 텍스트를 화면에
  출력해서 사람이 검토하게 함.
- `mcp_server/tools.py`에 `get_agent_analysis(ticker)` 추가 — **이 함수는
  DB에 캐싱된 걸 읽기만 하고 API를 호출하지 않는다.** 캐시가 없으면
  `run_agents.py`를 먼저 실행하라고 안내만 한다. `mcp_server/server.py`에
  등록해서 툴이 총 10개가 됨.
- `web/main.py` + `templates/index.html` — 대시보드에 "에이전트 리서치 요약"
  카드 추가. `build_dashboard_context()`가 캐싱된 결과만 읽어오므로 **대시보드를
  여는 것 자체는 API 비용이 절대 발생하지 않는다.** 캐시가 없으면 실행 안내
  문구를 보여줌.
- **검증 방법**: anthropic·pytest 둘 다 이 샌드박스에 설치할 수 없어서(pypi
  차단), (1) `.messages.create()`만 흉내내는 가짜 클라이언트로 tool-use 루프
  전체(정상 흐름·데이터 없음 에러 처리·금지 문구 탐지 2건·최대 반복 컷오프·
  토큰 사용량 합산)를 검증, (2) `db/store.py`의 멱등 저장을 실제로 두 번 써서
  행이 안 늘어나는지 확인, (3) `mcp_server/tools.get_agent_analysis`를 캐시
  있음/없음 양쪽 다 확인, (4) 기존 fastapi 스텁 기법으로 `web/main.py`가
  실제로 에이전트 카드를 렌더링하는지(캐시 있을 때·없을 때 폴백 문구 둘 다)
  end-to-end로 확인. `scripts/run_agents.py --dry-run`도 실제로 실행해서
  전송될 프롬프트를 눈으로 확인했고, API 키 없이 실행했을 때 네트워크 시도
  없이 바로 종료되는 것도 확인함(비용 0으로 유지 — 사용자가 "결제하지 말고"
  라고 명시했기 때문에 이 세션에서는 진짜 API를 한 번도 부르지 않았다).

### 이번 세션에서 조율한 원칙 충돌 (다음에 또 나올 수 있으니 기록)

- **"오늘 오를지 내릴지 예측해줘" / "확률로 보여줘" / "특정 가격 도달 확률 요청"**: 전부 거절함.
  확률이나 목표가로 완곡하게 표현해도 "미래 가격을 전망한다"는 본질은 같다고 보고
  원칙 1번을 그대로 적용했다. 대신 ADR 프리미엄·VIX 게이지·변동성처럼 **과거/현재
  관측값**만 보여주는 선에서 타협.
- **"워런 버핏·피터 린치 실명으로 에이전트 만들어줘"**: 사용자가 "나 혼자 보는
  거라 편의상"이라며 재요청했지만, **실명 그대로는 만들지 않음**. 실존 인물 이름을
  걸고 지어낸 1인칭 발언(예: "버핏이 오늘 이렇게 말한다")을 만드는 건 청중 규모와
  무관하게 인격권·오인 소지 문제라고 판단. 대신 "그레이엄·버핏 프레임워크"처럼
  **프레임워크 이름만 참고**하고 1인칭 발언은 만들지 않는 절충안으로 진행 —
  `analysis/frameworks.py`. 사용자가 이후 별다른 반대 없이 진행에 동의함.
- **"하이닉스 관련 소문도 포함해줘"**: 사용자가 "미확인 소문도 포함" 선택. 소문
  자체를 지어내지는 않고(그건 허위정보 생성이라 별개 문제), 기록 인프라만
  만들어둠 — `confidence='rumor'`로 저장하면 화면에 항상 "미확인" 라벨이 크게
  붙는다. 실제 소문을 채워넣는 건 `scripts/add_claim.py`로 사용자가 직접 하거나,
  다음 세션에서 뉴스 수집기를 확장하는 방향으로.

### 완료 (2026-08-24 세션 — 전문가 발언/소문 자동 채점)

claims 테이블에는 발언을 기록하는 기능(`scripts/add_claim.py`)만 있었고 채점하는
기능이 없었다. 이번 세션에서 사후 채점 파이프라인을 추가했다. 스키마 변경은
필요 없었다 — `graded_on`/`price_after`/`hit` 컬럼이 이미 있었다.

- `db/store.py`에 `get_claims_for_grading()`(채점 후보 조회, `include_graded`로
  --force 여부 결정) · `grade_claim()`(갱신) · `get_claim_scoreboard()`(적중률·
  표본수·미채점 건수·최근 결과 집계, web/MCP 공통 사용) 추가. `MIN_SAMPLES_CLAIMS
  = 30` — correlation/backtest처럼 모듈별로 독립된 상수를 둔다(기존 관행 유지,
  모듈 간 의존 안 만듦).
- `scripts/grade_claims.py` 신규 CLI. 판정 로직(`_judge`)은 순수 함수로 분리해
  테스트 가능하게 함:
  - 상승 방향(target_price > price_then): price_after >= target_price면 hit
  - 하락 방향(target_price < price_then): price_after <= target_price면 hit
  - target_price == price_then(방향 불명)은 **miss(0)로 처리** — 설계 판단:
    방향을 제시하지 않은 발언을 관대하게 hit 처리하면 채점 기준 자체가
    무의미해진다고 봄.
  - said_on+horizon_days(달력일) 시점 또는 그 이후 가장 가까운 거래일 종가를
    price_after로 채운다. 그 시점 이후 시세가 아직 DB에 없으면(수집 안 됨)
    조용히 건너뛰고 사유별 건수로 요약에 표시 — 잘못 채점하는 것보다 안전.
  - `--force` 없이는 이미 채점된 건(graded_on NOT NULL)을 후보에서 아예
    제외한다(원칙 6, `get_claims_for_grading(include_graded=False)` 기본값).
  - `--dry-run`으로 DB에 쓰지 않고 미리보기 가능.
- `mcp_server/tools.py`에 `get_claim_scoreboard()` 추가(조회 전용, 총 11개
  툴), `mcp_server/server.py`에 등록.
- `web/main.py`·`web/templates/index.html` — "전문가 발언 채점 현황" 카드 추가.
  적중률·표본수(30 미만이면 경고 태그)·미채점 건수·최근 채점 결과(confidence=
  'rumor'는 항상 "미확인" 라벨)를 보여준다. `claim_scoreboard`는 000660 시세
  유무와 무관하게 항상 계산은 되지만, 카드 자체는 기존 다른 카드들(뉴스·
  프레임워크·에이전트 요약 등)과 마찬가지로 `has_data`(000660 시세 있음) 조건
  안에서만 렌더링된다 — 기존 템플릿 구조를 그대로 따름.
- 테스트: `tests/test_grade_claims.py` 신규(상승/하락 적중·실패, 시세 없어서
  스킵, 만기 미도래 스킵, 다음 거래일로 대체, --force 없이 재채점 안 됨,
  --dry-run이 DB 안 건드림), `tests/test_mcp_server.py`·`tests/test_web.py`에
  scoreboard 테스트 추가.
- **검증 방법이 이전 세션들과 달라짐**: 이번 세션은 `pip install`이 실제로
  됐다(이전 세션들의 CLAUDE.md 기록에는 pypi 접근이 막혀 있었다고 되어
  있었는데, 이번엔 그렇지 않았다 — 샌드박스 정책이 바뀐 것으로 보임).
  그래서 스텁을 안 쓰고 pandas·fastapi·jinja2·mcp를 실제로 설치해서
  `pytest -q`를 그대로 돌렸다(61 passed). `web/main.py`의 실제 Jinja2
  템플릿도 진짜로 렌더링해서 확인함(합성 데이터, has_data=True/False 양쪽,
  "미확인" 라벨·nan 미검출·예측성 문구 없음 전부 실제 렌더 결과로 확인).
- **부수적으로 발견(내 변경과 무관, 기존 문제)**: `mcp>=1.2`로 설치하면
  pip이 mcp 2.0.0을 받아오는데, 이 버전은 `mcp.server.fastmcp` 모듈 자체가
  없어져서(`FastMCP` API가 없음) `mcp_server/server.py`가 임포트 단계에서
  깨진다. `tests/test_mcp_server.py`의 `test_server_module_registers_all_
  tools_when_mcp_installed` 하나가 이 때문에 실패한다(내가 추가한
  `get_claim_scoreboard` 등록 줄과는 무관 — 그 줄에 도달하기 전에 임포트가
  깨짐). `mcp_server/tools.py`(mcp 패키지 비의존, 순수 함수)는 전혀 문제
  없다. 고치려면 `requirements.txt`의 `mcp>=1.2`를 실제로 쓰는 버전대로
  핀(`mcp>=1.2,<2.0` 등)하거나, `server.py`를 mcp 2.x의 새 서버 API로
  마이그레이션해야 한다 — 이번 세션 범위 밖이라 손대지 않음.

### 완료 (2026-08-24 세션 2차 — 수집 통합 진입점)

`us_market.py`·`kr_market.py`·`news.py`를 매번 따로 돌려야 했던 걸 하나로
묶었다. GitHub Actions 자동화를 염두에 둔 설계.

- `collectors/collect_daily.py` 신규. `collect_daily(period="1y")`가 세
  수집기의 `collect_all()`을 **순차** 실행하고(디버깅하기 쉬워서 병렬 대신
  순차로 선택) `(job, ok, rows, error)` 리스트를 반환한다.
  - 한 수집기가 예외를 던져도 `try/except`로 감싸서 나머지는 계속 실행된다
    — job 하나 실패가 전체를 막지 않는다.
  - `--period`는 `us_market`/`kr_market`에만 전달한다(`news.collect_all()`은
    기간 인자가 없음, `WATCH_TICKERS`별 최신 헤드라인만 가져오는 구조라서).
  - 실행마다 `store.log_run(job, "ok"/"fail", rows, message)`을 **이 스크립트
    자신이** 직접 호출해서 `collect_log`에 남긴다. 각 `collect_all()`도
    내부적으로 종목 단위 로그를 이미 남기지만 그건 세부 기록이고, 이건
    "이 배치 실행 자체가 됐는가"를 job 단위로 남기는 별도 기록이다 —
    `collect_log`는 원래 누적 로그가 목적이라 이런 중복은 원칙 6 위반이
    아니라고 판단했다(반면 `daily_price`/`news_items`는 여전히 각
    `collect_all()`의 `upsert_*` 호출이 `ON CONFLICT`로 멱등하게 처리한다 —
    이 스크립트가 새로 건드리는 부분이 없다).
  - 하나라도 실패하면 `sys.exit(1)` — 부분/전체 실패는 구분하지 않는다.
- 테스트 `tests/test_collect_daily.py` 신규: 세 `collect_all`을 각각
  monkeypatch로 대체해서(실제 yfinance 호출 없음) 정상 시 `collect_log`
  3건 기록·하나 실패해도 나머지 둘은 정상 기록·`--period` 전달 확인·
  성공 시 exit 0/실패 시 exit 비0·요약 출력에 예측성 문구 없음을 검증.
  `pytest -q` 67 passed(기존 mcp SDK 버전 이슈 1건 제외 — 이 세션과 무관,
  위 5주차 세션 기록 참고).
- README.md "시작하기"에 `collect_daily.py` 사용법 추가(개별 실행 명령도
  디버깅용으로 남겨둠), 구조 다이어그램에도 반영.

### 완료 (2026-08-24 세션 3차 — mcp 버전 핀 수정)

`requirements.txt`의 `mcp>=1.2`가 mcp 2.0.0을 끌어와서 `mcp_server/server.py`
임포트가 깨지던 문제(직전 세션에서 발견만 하고 미수정) 해결.

- `requirements.txt`: `mcp>=1.2` → `mcp>=1.2,<2.0`. mcp 2.x는
  `mcp.server.fastmcp.FastMCP` 자체가 없어져서 API가 호환되지 않는다 —
  당장은 SDK를 마이그레이션하기보다 이 프로젝트가 실제로 검증한 1.x대에
  묶어두는 쪽을 택함(`mcp_server/server.py` 코드 자체는 그대로).
- 실제로 mcp 2.0.0 제거 후 `mcp>=1.2,<2.0` 재설치(1.29.0이 잡힘) →
  `mcp_server/server.py`가 정상 임포트되고 `FastMCP.list_tools()`로 11개
  툴이 전부 등록되는 것을 실제로 확인. `python -m mcp_server.server`도
  실행해서 예외 없이 뜨는 것까지 확인(stdio 서버라 입력 없이는 바로 종료됨,
  크래시 없이 종료되는 것만 확인 — Claude Desktop/Code에 실제로 붙여서
  툴 호출까지 해보는 건 여전히 미완).
- `pytest -q` 68 passed — 그동안 실패하던 `test_server_module_registers_
  all_tools_when_mcp_installed`도 이제 통과한다(이전 세션들엔 mcp SDK 설치가
  막혀 있어서 이 테스트가 한 번도 실행된 적이 없었다).

### 완료 (2026-10-05 세션 — 기술적 지표 규칙 백테스트)

사용자가 "기술적 지표 규칙 백테스트" 기능을 요청. 이번 단계는 **과거 데이터
백테스트만**이고, 주문/매매 실행 코드와 실시간 "오늘의 신호" 출력은 의도적으로
만들지 않았다 — 원칙 2(매매 기능 미구현) 그대로 유지. 규칙 백테스트 자체는
**과거 시점에 대한 측정**이라 원칙 1(예측 금지)과 충돌하지 않는다고 판단했다
(이벤트 백테스트 `analysis/backtest.py`와 같은 논리).

- `analysis/indicators.py` 신규 — SMA·RSI(Wilder 지수평활 방식)·볼린저밴드·
  MACD. `analysis/correlation.py`처럼 순수 함수, DB/API 비의존.
- `analysis/strategies.py` 신규 — `ma_cross`·`rsi_reversion`·
  `bollinger_reversion`·`macd_cross`·`ensemble`(4개 평균 점수 threshold).
  전부 롱 전용(공매도 없음), "보유 여부"(1/0) 시리즈를 돌려준다. **파라미터는
  함수 인자로 노출하되 이번엔 기본값만 사용** — 적은 데이터로 파라미터를
  데이터에 맞춰 고르면 과거에만 맞는 우연한 조합을 "전략"으로 착각하기 쉽다
  (과최적화 방지, 코드 주석에도 명시).
- `analysis/rule_backtest.py` 신규(기존 `analysis/backtest.py`는 이벤트
  백테스트 전용이라 그대로 두고 분리) — 핵심은 **체결 시점**: t일 종가로
  계산한 신호는 t+1일 시가(없으면 t+1일 종가)에 체결된다고 가정한다.
  `holding.shift(1)`로 "실제 보유 상태"를 만들어 구현했고, 이게 지켜지는지
  `tests/test_strategies.py`의 `TestRuleBacktestExecutionTiming`에서 숫자로
  직접 검증한다(결정일 종가로 체결됐다면 나올 값과 다름을 확인).
  - 거래비용: 매수/매도 수수료율 + 매도 시 세금률을 인자로 받음. 기본값
    수수료 0.015%(편도)·매도세 0.20% — **참고용 근사치, 실제 요율은
    증권사·연도별로 다르니 확인 필요**(코드 주석에도 명시).
  - 결과 지표: 총수익률·연환산수익률(CAGR)·MDD·거래 횟수(완료된
    라운드트립만, n)·승률·평균수익/평균손실·최악의 단일 거래·노출도(보유
    기간 비율). `n < 30`이면 `reliable=False`(원칙 3, 기존 `MIN_SAMPLES`
    관례 그대로). 기간을 앞 70%(in-sample)/뒤 30%(out-of-sample)로 나눠
    각각 따로 낸다 — 지표는 전체 종가로 한 번만 계산하고, 거래의 **진입
    날짜**가 어느 구간에 속하는지로만 나눈다(구간을 잘라서 지표를 다시
    계산하면 이동평균 등이 구간 앞쪽에서 불필요하게 NaN이 됨).
  - buy & hold 비교는 별도 코드 경로를 안 만들고 같은 엔진에
    `holding=전부 1`을 넣어서 재사용했다 — 체결 지연(t+1 시가) 규칙이
    buy&hold에도 동일하게 적용되어 일관성이 생긴다. buy&hold는 한 번
    사서 안 파니까 완료된 라운드트립이 0건이라 `n=0`/`reliable=False`로
    나오는데, 이건 버그가 아니라 설계대로다(총수익률·CAGR·MDD는 정상
    계산됨, 승률 등 거래 단위 지표만 해당 없음을 뜻함).
- `db/store.py`에 `get_ohlc(ticker)` 추가 — 기존 `get_prices()`는 종가만
  줘서 체결가(시가)를 못 구했다. `get_prices()`는 다른 모듈(상관계수 등)이
  종가만 쓰므로 그대로 두고 새 함수만 추가(원칙: 기존 쓰는 곳 안 건드림).
- `scripts/backtest_strategies.py` 신규 CLI. 기본 종목 000660,
  `--ticker`/`--start`/`--end`/`--threshold` 인자. DB 데이터가
  `MIN_ROWS=250`일(대략 1년) 미만이면 `collectors/kr_market.py 5y`(또는
  `max`) 실행을 안내하고 종료(에러로 죽지 않고 사용자가 할 일을 알려줌).
  전체/in-sample/out-of-sample 세 표를 각각 출력, 각 표에 5개 규칙 +
  ensemble 0.75 비교행 + buy&hold를 수익률과 **같은 비중으로 MDD를 나란히**
  표시(원칙 4). `reliable=False`인 행에는 경고 표시. 맨 아래 고정 문구
  "과거 데이터에 대한 측정 결과이며 향후 수익을 보장하지 않습니다. 이 결과는
  매수·매도 추천이 아닙니다." 출력. 목표가·확률·"지금 사라/팔아라" 같은
  예측성 문구는 어디에도 넣지 않았다.
- `tests/test_strategies.py` 신규(22개, 실제 yfinance 호출 없이 합성
  데이터) — 지표 알려진 값 검증, 미래 참조 없음(마지막 날 값을 바꿔도
  이전 결과 불변 — 지표/전략 9개 함수 파라미터화해서 반복 검증), 체결
  시점(핵심 — t+1 시가 체결을 숫자로 확인, 시가 없을 때 종가 폴백도),
  ensemble threshold 동작(monkeypatch로 4개 투표를 고정해서 검증), 거래비용이
  실제로 수익률을 깎는지, n<30일 때 `reliable=False`인지.
- **검증 방법**: 이 세션은 네트워크/pypi 접근이 막혀 있지 않아서
  `collectors/kr_market.py 5y`를 실제로 돌려 000660 5년치를 수집하고
  `scripts/backtest_strategies.py`를 실제 데이터로 실행해서 표가 깨지지
  않고 출력되는지, `--threshold`/`--start`/`--end` 인자가 실제로 반영되는지
  눈으로 확인했다. `pytest -q` 전체 90 passed(기존 68 + 신규 22).
- **아직 안 한 것 / 다음에 생각할 거리**:
  - 대시보드(`web/main.py`)·MCP 서버에는 이번 기능을 연결하지 않았다.
    요청 범위가 백테스트 자체였고, 대시보드에 붙이려면 "n<30 경고를
    화면에서도 숨기지 않기" 원칙을 다시 지켜야 해서 별도 작업으로 남김.
  - 실매매 연동·알림("오늘의 신호") 단계는 **의도적으로 미구현**이다.
    이 프로젝트 원칙 2(매매 기능 미구현) 때문에 지금 범위에는 없고, 나중에
    혹시 진행하게 되더라도 **먼저 모의투자 계좌로 검증할 것** — 실제 계좌
    연동은 그 다음.
  - 거래비용 기본값(수수료 0.015%, 세금 0.20%)은 참고용 근사치다. 실제
    사용 전 본인 증권사·연도 기준으로 다시 확인할 것.

### 완료 (2026-10-05 세션 2차 — 규칙 백테스트를 대시보드·MCP에 연결)

직전 세션에서 만든 `scripts/backtest_strategies.py`(CLI 전용)를 대시보드와
MCP 서버에도 노출했다. 이번에도 매매 실행·실시간 신호는 추가하지 않았다
(원칙 2 그대로) — 둘 다 **과거 데이터 조회+계산**만 한다.

- `mcp_server/tools.py`에 `get_rule_backtest(ticker, strategy, threshold)` 추가
  (총 12개 툴). `strategy`는 `ma_cross`·`rsi_reversion`·`bollinger_reversion`·
  `macd_cross`·`ensemble` 중 하나, 알 수 없는 값은 `ValueError`. 데이터가
  `RULE_BACKTEST_MIN_ROWS=250`일 미만이면 기존 `_require_prices` 패턴처럼
  `DataNotFoundError`로 수집 안내. 반환값은 `full`/`in_sample`/`out_of_sample`
  세 구간 모두 담고, 각 구간에 `n`·`reliable`·`mdd_pct`를 항상 포함(원칙 3·4).
  docstring에 "매매 신호가 아니라 과거 측정"임을 명시하고, `mcp_server/server.py`
  INSTRUCTIONS에도 이 툴이 일반 가이드라인(방향성 판단 금지·n<30 고지·매매
  기능 없음) 대상이라고 한 줄 추가. `tests/test_mcp_server.py`에 5개 테스트
  추가(데이터 부족/알 수 없는 strategy 에러, 세 구간 모두 반환, ensemble
  threshold 그대로 반영, 예측·매매 문구 없음).
- `web/main.py`의 `build_dashboard_context()`에 "기술적 지표 규칙 백테스트"
  섹션 추가 — 000660 전체 기간 기준 5개 규칙 + buy_and_hold 6행. 대시보드는
  한눈에 보는 요약용이라 **전체 기간만** 보여주고(in-sample/out-of-sample
  분리·threshold 0.75 비교처럼 더 깊은 비교는 CLI를 쓰라고 카드 안에 안내
  문구를 남겨둠), 데이터가 `RULE_BACKTEST_MIN_ROWS`(250일) 미만이면 카드
  대신 수집 안내를 보여준다. `web/templates/index.html`에 표 카드 추가 —
  총수익률 옆에 **최대낙폭(MDD)을 같은 비중으로 나란히** 배치(원칙 4),
  `reliable=False` 행에는 다른 카드들과 같은 `tag warn` 스타일로 "n<30"
  표시. footer 고정 문구에도 "매수·매도 추천 아님 / 거래비용은 참고용
  근사치" 한 줄 추가. `tests/test_web.py`에 2개 테스트 추가(데이터 짧으면
  카드가 `None`인지, 채워지면 6행 모두 `mdd_pct`/`reliable`을 갖는지).
- **검증 방법**: 실제 수집된 000660 5년치 DB로 `build_dashboard_context()`
  직접 호출, 진짜 Jinja2 템플릿으로 `index.html` 렌더링(값이 CLI 출력과
  정확히 일치함을 확인, "nan" 문자열 없음, 예측·매매 문구 없음), 그리고
  `mcp_server.tools.get_rule_backtest()`를 직접 호출해 CLI와 동일한 숫자가
  나오는지 확인했다. `pytest -q` 전체 97 passed(기존 90 + 신규 7).
- **아직 안 한 것**: `uvicorn web.main:app --reload`로 브라우저에서 직접
  열어본 적은 없음(이전 세션들과 같은 미확인 상태 — "기술적 지표 규칙
  백테스트" 카드가 실제 화면에서 레이아웃이 안 깨지는지는 직접 확인
  필요). Claude Desktop/Code에 MCP 서버를 실제로 연결해서
  `get_rule_backtest` 툴 호출까지 end-to-end로 확인하는 것도 기존 미완
  항목(다른 10개 툴과 동일한 상태)에 포함됨.

### 완료 (2026-10-05 세션 3차 — 눌림목 지정가 매매 규칙 백테스트)

사용자가 "눌림목 지정가 매매" 백테스트를 요청. 기존 규칙 백테스트
(SMA/RSI/볼린저/MACD 보유 시리즈 + `analysis/rule_backtest.py`의 shift(1)
엔진)와 신호의 모양이 근본적으로 달라서 — 포지션 없을 때만 매일 새 지정가를
걸고, 체결/손절/익절 판정이 그날의 저가~고가 구간 전체를 봐야 하고, 한 번에
1포지션으로 진입~청산이 여러 날 이어짐 — "보유 시리즈 하나 + 범용 엔진"
패턴에 끼워 넣지 않고 전용 상태기계로 새로 짰다. 이번에도 주문/매매 실행
코드는 안 만들었다(원칙 2).

- `analysis/pullback.py` 신규, 순수 함수:
  - `simulate_pullback_trades()` — 날짜순 상태기계. 진입: 포지션 없을 때만,
    갭상승(gap_skip_pct, 기본 0.02) 이상이면 그날 주문 자체를 안 냄, 지정가 =
    전일종가*(1-dip_pct)(기본 0.02), 시가가 이미 지정가보다 낮으면 시가
    체결·아니면 저가가 지정가에 닿으면 지정가 체결. 청산: 익절(기본 +2%,
    고가 기준)·손절(기본 -3%, 저가 기준, `stop_loss_pct=None`이면 꺼짐)·
    `max_hold_days`(기본 10) 초과 시 종가 강제청산. **같은 날 손절·익절이
    둘 다 닿으면 손절을 우선 체결된 것으로 본다** — 일봉만으로는 장중 순서를
    알 수 없어서, 유리한 쪽을 가정해 결과를 부풀리지 않으려는 보수적 선택
    (주석에 명시). 진입 당일은 그날 바로 손절/익절 체크를 안 함(하루 단위
    단순화 — 동시에 일어날 수 있지만 일봉만으론 순서를 알 수 없다는 같은 이유).
  - `classify_regime()` — 직전 N거래일(기본 20) 수익률로 상승추세(+8%
    초과)/박스권/하락추세(-8% 미만) 분류, 임계값·lookback 전부 파라미터화.
    트레일링 계산이라 미래 참조 없음.
  - `regime_breakdown()` — 각 거래를 진입일 장세로 묶어 n/승률/평균손익/
    최악거래/총손익(그 장세 거래만 진입일 순서로 체이닝한 복리)/MDD를 내고,
    **같은 장세로 분류된 날짜만 보유했을 때의 buy&hold 수익률**도 같이
    낸다(기회비용 비교 — 실제로 상승추세 구간에서 이 규칙이 단순 보유 대비
    크게 못 미쳤다는 게 숫자로 드러남, 아래 검증 결과 참고). n<30이면
    reliable=False.
  - `backtest_pullback()` / `summarize_pullback_trades()` — 결과를
    `analysis/rule_backtest.RuleBacktestResult`로 그대로 반환한다(새
    dataclass를 또 안 만들고 재사용) — 매매 방식이 달라도 지표의 모양
    (n/승률/MDD/reliable)은 같아야 하고, 같아야 `scripts/backtest_strategies.py`
    의 표 출력 코드를 그대로 재사용할 수 있어서. 이를 위해
    `analysis/rule_backtest.py`의 `_result_for_window()`를 `result_for_window()`
    로 공개 함수화(호출부 3곳만 있던 내부 함수라 이름만 바꿈, 동작 변경 없음).
  - 거래비용(수수료/세금)은 `analysis/rule_backtest.py`의 기본값을 그대로
    가져다 씀(요청대로 "기존 백테스트와 같은 파라미터").
- `db/store.py`의 `get_ohlc()`를 open/close만 → **open/high/low/close**로
  확장 — 지정가 체결·손절·익절 판정에 그날 저가/고가가 필요해서. 기존
  호출부(scripts/backtest_strategies.py, mcp_server/tools.py, web/main.py)는
  전부 컬럼명으로 골라 쓰므로 컬럼이 늘어도 영향 없음(실제로 기존 108개
  테스트 그대로 통과 확인).
- `scripts/backtest_strategies.py`에 `--strategy {all,pullback}` 옵션 추가
  (기본 `all` — 기존 동작 그대로, 실제 출력이 바이트 단위로 동일함을 직접
  비교 확인). `--strategy pullback`이면:
  - 손절 있는(기본) 버전과 **손절 없는 버전(stop_loss_pct=None)을 항상 같이**
    전체/in-sample/out-of-sample 세 표에 나란히 출력 — 요청대로 "실제로는
    손절을 안 지키는 경우가 많아서 그게 결과를 얼마나 바꾸는지" 비교.
  - 장세별 성과 표(전체 기간, 손절 있는 버전 기준)를 따로 출력. MDD를
    총손익과 같은 비중으로 나란히 배치(원칙 4), n<30 경고 유지(원칙 3).
  - 눌림목 전용 파라미터 8개를 CLI 인자로 노출(`--dip-pct` 등), 전부 기본값
    그대로 써도 동작.
  - 고정 면책 문구는 분기와 무관하게 마지막에 한 번만 출력되도록 공통화.
- `tests/test_pullback.py` 신규(11개, 합성 OHLC, yfinance 호출 없음) —
  지정가 체결(시가 체결/저가 터치 체결/체결 안 됨 3가지), 갭상승 스킵(갭
  초과 시 저가가 떨어져도 체결 안 됨 vs 임계값 미달이면 정상 주문),
  같은 날 손절·익절 동시 터치 시 손절 우선, 손절 비활성화 시 절대 안
  터지는지, max_hold_days 강제 청산, 장세 분류(손으로 계산한 가상 수익률
  기준)와 마지막 날 값을 바꿔도 이전 분류가 안 바뀌는지.
- **검증 방법**: 실제 000660 5년치 DB로 `scripts/backtest_strategies.py`
  (기본 `all` 모드와 `--strategy pullback` 둘 다) 실제 실행. 손절 3%/take
  2%/dip 2%라는 타이트한 기본값이 이 종목 5년 구간엔 잘 안 맞아서 둘 다
  손실(-55%/-5.9%)로 나왔고, 특히 **장세별 리포트에서 상승추세 구간 buy&hold
  +9204% vs 이 규칙의 그 구간 총손익 -2.85%**로 기회비용이 극적으로 드러남
  — 설계 의도(규칙이 상승장에서 보유 대비 얼마나 놓치는지 보여주기)가 실제
  숫자로 확인됨. `--dip-pct`/`--take-profit-pct`/`--max-hold-days`/`--start`/
  `--end`도 직접 바꿔서 값이 반영되는지 확인. `pytest -q` 전체 108 passed
  (기존 97 + 신규 11).
- **아직 안 한 것**: 이번엔 MCP/대시보드에는 연결하지 않았다 — 요청 범위가
  CLI까지였음. 필요하면 직전 세션의 `get_rule_backtest` 패턴을 그대로 따라
  `get_pullback_backtest` 같은 MCP 툴을 추가할 수 있음(RuleBacktestResult를
  그대로 재사용하므로 직렬화 코드도 거의 복붙 가능할 것). 이 기본
  파라미터(dip 2%/익절 2%/손절 3%)가 실전에 맞는지는 전혀 검증된 바 없고,
  의도적으로 파라미터 최적화를 안 했으므로 사용자가 직접 여러 값을 시도해볼
  것(과최적화 방지 목적, analysis/strategies.py와 같은 이유).

### 완료 (2026-10-05 세션 4차 — 장세 분류 겹침 버그 수정 + 전 기간/삼성전자 확장)

사용자가 직전 세션의 `regime_breakdown()`이 "분류에 쓴 날짜"와 "buy&hold
수익률을 측정한 날짜"가 겹치는지 확인해달라고 요청. 실제로 버그였다 — 바로
고치고, 이어서 같은 대화에서 추가로 (1) 000660을 `period=max`로 더 길게
수집, 2000~2006년 이상치 확인, 백테스트 기본 시작일 2005-01-01 적용,
(2) 005930(삼성전자) 수집기 추가, (3) 장세별 표에 기대값(평균손익)·중앙값
추가를 요청해서 같이 처리했다.

- **버그 수정 (`analysis/pullback.py`의 `regime_breakdown()`)**: `regime[t]`는
  t일 종가까지의 데이터로 정한 분류인데, 기존 코드는 그 분류에 쓰인 바로
  그날(t)의 수익률(`close.pct_change()`, 전일종가→t일종가)을 그대로 buy&hold
  집계에 썼다. 예를 들어 "그날 +30% 급등 때문에 그날이 상승추세로 분류됐는데,
  그 +30%가 다시 상승추세 buy&hold 수익으로 집계"되는 순환 참조였다 —
  재현 스크립트로 직접 확인(합성 데이터로 regime[t]="uptrend" 유도 → 버그
  버전 30%, 수정 버전 0%). 수정: `next_day_ret = close.pct_change().shift(-1)`
  로 바꿔서 **regime[t]엔 t→t+1(다음날) 수익률**을 붙인다 — 신호 체결 지연
  원칙(이 프로젝트 전체가 "close[t] 신호 → t+1일 효과"로 통일)과 일치시킴.
  마지막 날은 다음날 수익률이 없어 자연히 제외(에러 아님). trades 쪽(거래당
  성적)은 원래부터 entry_date 분류와 거래 수익이 entry 다음날부터 실현되는
  구조라 겹침이 없었음(점검 결과 해당 없음, 고칠 필요 없었음).
  `tests/test_pullback.py`에 회귀 테스트 2개 추가(분류를 만든 당일 수익률이
  안 섞여 들어가는지, 마지막 날이 조용히 제외되는지).
- **장세별 표에 중앙값 추가**: `RegimeBreakdown`에 `median_return_pct` 필드
  추가(`statistics.median`). 평균(기대값)·중앙값·최악거래는 **buy&hold와
  무관하게 규칙 자체의 거래당 성적**이라고 docstring과 CLI 헤더 주석에
  명시 — 둘을 같은 표에 놓긴 하지만 서로의 계산에 쓰이지 않는다. 실제로
  000660/005930 둘 다 평균(기대값)은 음수인데 중앙값은 +1.77%로 나와서
  — "전형적인 거래는 작게 이기지만, 가끔의 손절(-3.22%)이 평균을 끌어내려
  장기적으로는 음의 기대값"이라는 걸 median vs mean 차이로 보여줌. CLI
  표(`scripts/backtest_strategies.py`의 `_print_regime_table`)에 중앙값
  컬럼 추가. `tests/test_pullback.py`에 평균/중앙값/최악거래가 buy&hold
  계산과 독립적으로 나오는지 테스트 추가.
- **000660 `period=max` 수집 + 이상치 확인**: `collectors/kr_market.py max`
  실행 → 2000-01-04부터 6686행 확보(이전엔 5y만 있었음). 2000~2006년 구간을
  직접 스캔한 결과 **2001-11-12에 +32.08% 단일 거래일 변동 1건** 발견 —
  당시 KRX 일일 가격제한(역사적으로 ±15% 수준)을 넘는 값이라 정상적인
  장중 거래로는 설명이 안 되고, 하이닉스가 2001년 전후 극심한 재무위기·
  구조조정(2003-04-14 약 1:21 무상감자가 yfinance actions에 기록돼 있음,
  단 이 감자는 저장된 close 시리즈엔 불연속 없이 이미 반영돼 있어 별도
  보정 불필요함을 확인)을 겪은 시기와 맞아떨어져 데이터 보정/거래정지 재개
  흔적으로 추정된다(확정은 아님). 2005-01-01 이후로는 000660·005930 둘 다
  2000~2026 전체 구간에서 ±30% 초과 이상치가 전혀 없음을 확인.
- **백테스트 기본 시작일 2005-01-01**: `scripts/backtest_strategies.py`의
  `--start` 기본값을 `None`에서 `DEFAULT_START = "2005-01-01"`로 변경(위
  이상치 구간을 기본값에서 피함). `--start ''`로 비우면 여전히 전체 기간
  사용 가능(빈 문자열은 falsy라 필터 안 걸림, 기존 `if args.start:` 로직
  그대로 재사용). 이 기본값 변경으로 n(거래 횟수)이 크게 늘어서(예: 000660
  ma_cross 39회→170회) 통계적 신뢰도가 전반적으로 개선됨.
- **005930(삼성전자) 추가**: `collectors/kr_market.py`의 `TICKERS`에
  `"005930.KS": "005930"` 추가, `period=max`로 수집(6686행, 000660과 같은
  범위, 2000-2006년 구간엔 이상치 없음 확인). `scripts/backtest_strategies.py`
  는 이미 `--ticker` 인자가 있어서 코드 변경 없이 `--ticker 005930`으로
  바로 동작.
- **검증 방법**: `--ticker 000660`/`--ticker 005930` 각각 `--strategy all`·
  `--strategy pullback` 전부 실제 실행해서 표가 깨지지 않고 새 기본 시작일
  (2005-01-01)·중앙값 컬럼이 반영되는지 확인. 특히 전체 기간(21년)으로
  늘어나면서 pullback 전략(고정 2%/3% 손익비, 복리 재투자)이 거래 1100여
  건을 거치며 누적 손실이 -99%에 가까워지는 결과가 나왔는데, 승률 57%·평균
  손실 -3.22%(손절가 고정이라 분산 없음)·평균수익 +1.77%로 기대값을 손으로
  계산해보면(0.57*1.77 - 0.43*3.22 ≈ -0.37%/거래) 1100거래 복리 compounding과
  일치함을 확인 — 버그가 아니라 "손절이 익절보다 커서 거래 횟수가 많아지면
  망한다"는 이 전략 설계 자체의 특성이 숫자로 드러난 것. `pytest -q` 전체
  111 passed(기존 108 + 신규 3: 겹침 버그 회귀 2개, 기대값/중앙값 독립성 1개).
- **아직 안 한 것**: 2001-11-12 이상치의 정확한 원인(데이터 벤더 보정 vs
  실제 거래정지 재개 vs 다른 사유)은 확정하지 못했다 — 2005-01-01 기본
  시작일로 우회했을 뿐 원인 자체를 규명하진 않았다. 필요하면
  `daily_price`에서 그 날짜 전후 거래량(volume)도 같이 봐서 거래정지
  재개 패턴(거래량 급증/급감)인지 확인하는 게 다음 단서가 될 수 있음.
  005930 쪽은 KIS 수급 데이터(`investor_flow`)엔 아직 연결 안 함(000660만
  감시 종목으로 CLAUDE.md 기술 스택에 명시돼 있었음, 필요하면 `config.py`의
  `watch_kr` 등도 같이 확장 검토).

### 완료 (2026-10-06 세션 — 원칙 1 갱신: 백테스트 통과 규칙만 매수/목표/손절 기준가 표시)

사용자가 원칙 1을 "주가를 예측하지 않는다"에서 "백테스트로 검증된 규칙만
매수/매도 기준가로 표시한다"로 명시적으로 바꾸길 요청. 이 프로젝트의 가장
중요한 원칙을 뒤집는 요청이라 진행 전에 범위를 확인했다 — 사용자가 "LLM
가드레일·MCP·README 면책 문구도 같이 갱신"을 선택해서, 코드뿐 아니라
`agents/orchestrator.py`·`mcp_server/server.py`·README·이 파일까지 전부
일관되게 고쳤다. **주문 실행 코드는 이번에도 만들지 않았다**(원칙 2 유지,
사용자가 명시적으로 "주문 API는 아직 연결하지 마"라고 못 박음).

- `analysis/price_rules.py` 신규, 순수 함수 — 매수/목표/손절 기준가 규칙 3개:
  - `ma_pullback_levels` — 종가가 장기 SMA(60) 위(상승추세)일 때만, 짧은
    SMA(20)까지 되돌림 오면 그 지점에 지정가 매수. 추세 조건 미충족이면
    신호 없음(NaN).
  - `atr_band_levels` — 당일 종가에서 ATR(14)의 1.5배 아래 지정가 매수.
    변동성이 큰 날은 밴드가 넓어진다(고정 % 밴드와 다른 점). `analysis/
    indicators.py`에 `atr()`(Wilder 평활, rsi()와 같은 방식) 추가.
  - `breakout_range_levels` — 당일 고가를 돌파 매수 기준가, 당일 저가를
    손절가로 그대로 쓴다(전일 고저 돌파 규칙의 정의상 별도 비율 파라미터
    없음). 세 함수 모두 "t일 종가까지 데이터로 계산한 레벨 → t+1일 체결"
    원칙(미래 참조 금지)을 지키고, 그 시프트는 함수가 아니라 백테스트
    엔진이 일괄 적용한다(함수 자체는 그날의 레벨만 계산).
- `analysis/price_rule_backtest.py` 신규 — `analysis/pullback.py`와 거의
  같은 상태기계(같은 날 손절·익절 동시 터치 시 손절 우선, 진입 당일은
  체크 안 함 등 보수적 가정도 동일)지만, 손절/익절을 체결가에서 다시
  계산하지 않고 **신호 발생일에 이미 정해진 절대 가격**을 그대로 쓴다.
  진입 방식 두 가지: "limit"(저가가 닿으면 체결, ma_pullback·atr_band)과
  "stop"(고가가 넘으면 체결, breakout_range). 결과는 `rule_backtest.
  RuleBacktestResult`를 그대로 재사용(새 dataclass 또 안 만듦) —
  `rule_backtest.py`의 private `_result_for_window`를 `result_for_window`로,
  `pullback.py`의 private `_daily_returns_and_exposure`를
  `daily_returns_and_exposure`로 공개 함수화해서 재사용(동작 변경 없음,
  이름만). `rule_backtest.RuleBacktestResult`에 `mean_return`(거래당 평균,
  승+패 전부 포함) 속성 추가 — avg_win/avg_loss로는 "거래당 평균이
  플러스냐"를 바로 판정할 수 없어서 새로 필요했음.
  - `passes(results)` — **통과 판정 함수, 사용자가 명시한 기준 그대로**:
    전체 기간 거래 수 ≥100, 전체 기간 거래당 평균(`mean_return`) 양수,
    최근 1년(out-of-sample) 거래당 평균도 양수. 셋 중 하나라도 거짓이면
    False(거래 0건이라 NaN인 경우도 `NaN > 0`이 False라 자동으로 탈락).
  - 분할 기준이 기존 70/30 비율(`rule_backtest.py`·`pullback.py`)과
    다르다 — 사용자 요청대로 **달력 기준 최근 1년**을 검증 구간으로 뗀다
    (`out_of_sample_years=1`, 기본값).
- `scripts/backtest_strategies.py`에 `--strategy price_rules` 추가. 3개
  규칙 전체/검증제외/최근1년 표 + PASS/FAIL 열, 그리고 통과한 규칙만
  "오늘의 기준가"를 따로 출력(통과 못 하면 가격 자체를 안 보여줌).
- `web/main.py`에 "매수·목표·손절 기준가" 카드 추가. 통과한 규칙만 리스트에
  들어가고(`price_rule_signals`가 `None`=데이터 부족, `[]`=통과한 규칙
  없음→"오늘 해당 규칙 없음", 비어있지 않은 리스트=각 규칙의 오늘
  매수/목표/손절가 + 과거 성적 n·승률·거래당평균·MDD·최근1년 성적).
  `scripts/backtest_strategies.py`의 `DEFAULT_START`(2005-01-01, 000660
  2001년 이상치 구간 회피)와 똑같은 필터를 대시보드 쪽에도 새로
  적용했다(`PRICE_RULE_START`) — 처음엔 대시보드가 전체 2000년치를 그대로
  썼다가 CLI(392건)와 대시보드(484건) 숫자가 달라서 발견, 수정 후 CLI와
  정확히 일치 확인. 기존 "기술적 지표 규칙 백테스트" 카드는 이미 쓰던
  대로 둠(이번 요청 범위 밖, 건드리면 기존 동작이 바뀜).
- **가드레일/문서 일관성 갱신** (사용자가 범위에 포함하기로 선택):
  - `CLAUDE.md` 원칙 1을 위 내용으로 교체, LLM 서술에는 이 예외가 적용되지
    않는다는 걸 명시.
  - `agents/orchestrator.py` 모듈 docstring에 이 예외가 LLM 가드레일
    (`_scan_for_violations()`, 금지 문구 목록)과 무관하다는 설명 추가 —
    로직은 안 건드림, 여전히 "매수 시점" 같은 LLM 출력은 전부 차단.
  - `mcp_server/server.py`의 INSTRUCTIONS에 4번 항목 추가(참고용) — 이
    서버는 아직 해당 기능을 툴로 노출하지 않는다는 점, LLM이 서버 숫자로
    직접 "매수 시점"을 판단하는 건 여전히 금지라는 점 명시.
  - `README.md` 설계 원칙 1번·면책 섹션을 원칙 1 갱신 내용에 맞게 재작성,
    구조 다이어그램에 새 모듈 2개 추가, 알려진 제약에 새 섹션 추가(실제로
    3개 규칙 중 1개만 통과했다는 사실도 적어둠 — "이름만으로 다 통과하는
    게 아니다"를 분명히 하려고).
- `tests/test_price_rules.py` 신규(18개, 합성 OHLC) — ATR 계산·미래 참조
  없음, 3개 규칙의 레벨 계산(추세 필터·고저 직접 사용 등), limit/stop
  체결·같은 날 손절익절 동시 터치·max_hold 강제청산, 통과 판정 5가지
  시나리오(표본부족/전체마이너스/검증구간마이너스/검증구간거래없음/통과),
  최근1년 분할이 달력 기준으로 정확히 나뉘는지. `tests/test_web.py`에도
  2개 추가(데이터 부족 시 None, 충분하면 리스트고 통과 기준을 실제로
  만족하는지 — 합성 랜덤워크라 보통 빈 리스트로 나옴).
- **검증 방법**: 실제 000660(2005~) 데이터로 CLI `--strategy price_rules`
  실행 → 3개 중 "이동평균 눌림목" 1개만 통과(n=392, 거래당평균 +0.98%,
  최근1년 n=28·+4.33%), ATR 밴드·전일 고저 돌파는 거래당 평균이 음수라
  탈락. 오늘의 기준가(매수 1,782,050 / 목표 1,871,152 / 손절 1,728,588)가
  최근 종가(1,841,000)대비 합리적인 범위인지 직접 확인. 그 다음
  `build_dashboard_context()` + 진짜 Jinja2 템플릿으로 대시보드 카드를
  렌더링해서 **CLI와 정확히 같은 숫자**가 나오는지 확인(위에서 설명한
  PRICE_RULE_START 불일치 버그를 이 과정에서 발견·수정함). `pytest -q`
  전체 131 passed(기존 111 + 신규 20: price_rules 18개 + web 2개).
- **아직 안 한 것**: MCP 서버에는 이 기능을 툴로 아직 연결하지 않았다(참고
  문구만 추가, 요청 범위가 대시보드까지였음) — 필요하면 직전 세션의
  `get_rule_backtest` 패턴을 그대로 따라 `get_price_rule_signals` 같은
  툴을 추가할 수 있음. 005930 등 다른 종목에는 아직 이 카드를 안 띄움
  (대시보드는 000660 전용 구조 그대로 — 여러 종목으로 확장하려면
  `HYNIX` 하드코딩을 걷어내는 선행 작업이 필요해서 이번 범위 밖으로 둠).
  3개 규칙의 파라미터(SMA 60/20, ATR 1.5/2.0/1.0배, 돌파 목표 5%)는 전혀
  최적화하지 않았다 — 과최적화 방지 목적으로 의도적으로 기본값만 썼다.

### 완료 (2026-10-06 세션 2차 — 애널리스트/의견 추적, 기존 claims 확장)

사용자가 "애널리스트 의견 추적" 기능(날짜·이름·소속·출처·의견(매수/중립/매도)·
목표가·기간을 저장하는 `analyst_views` 테이블 + 수동 입력 CLI + 대시보드
카드)을 요청. 코드를 보니 이미 `claims` 테이블 + `scripts/add_claim.py` +
`scripts/grade_claims.py`가 거의 같은 걸 하고 있어서, 완전히 새 테이블을
만들기 전에 사용자에게 확인 — **기존 `claims` 확장**을 선택받았다(중복
테이블/CLI/대시보드 카드를 두 벌 만들지 않기 위해).

- `db/schema.sql`의 `claims`에 `affiliation`(소속)·`stance`(매수/중립/매도)
  컬럼 추가. `db/store.py`의 `_migrate()`에 두 컬럼 ALTER TABLE 추가
  (기존 DB에도 안전하게, 원칙 6).
- `db/store.py`:
  - `insert_claim()`이 `affiliation`/`stance`를 받도록 확장(기본값 None).
  - `get_latest_claims_by_person(ticker)` 신규 — 종목별로 사람(who)당 가장
    최근 발언 1건. **버그 주의해서 짚은 부분**: `groupby("who").first()`는
    컬럼별로 NaN을 건너뛰고 첫 값을 찾기 때문에, 최신 발언에 `target_price`
    가 없고 더 오래된 발언엔 있으면 둘이 섞여버린다(최신 summary + 오래된
    target_price). `head(1)`로 바꿔서 행 전체를 통째로 가져오게 고쳤고,
    이 정확한 시나리오를 테스트로 박아뒀다(`test_whole_row_is_kept_
    together_even_with_nulls`).
- `scripts/add_claim.py`에 `--affiliation`·`--stance`(매수/중립/매도, argparse
  choices로 제한) 인자 추가. 기존처럼 CLI 자체는 얇은 배선이라 직접
  테스트하지 않고(기존 관행, test_grade_claims.py 상단 설명 참고) 그
  아래 store 로직만 테스트했다.
- `web/main.py`의 `build_dashboard_context()`에 "애널리스트/의견 추적" 섹션
  추가 — `claim_scoreboard`와 같은 철학으로 000660 시세 유무와 무관하게
  항상 계산(채점 완료 여부와도 무관 — "최근 의견이 뭐였는지"는 채점 전에도
  보여줄 수 있어야 함). 가격 변화(`pct_change_since`)는 `price_then`(기록
  시점 종가, 보통 `add_claim.py`가 자동 채움) 대비 **현재 최신 종가**로
  계산 — `get_latest_claims_by_person()`은 조회만 하고 가격 조합은
  호출부가 한다(기존 store.py 계층 분리 관행 그대로).
  `web/templates/index.html`에 카드 추가 — 사람(소속) + 매수/중립/매도
  태그 + 요약(출처 링크) + 목표가/발언시점가/현재가/변화율. 원문 인용 없음,
  summary는 여전히 본인 요약 한 줄(원칙 5 그대로).
- `tests/test_analyst_views.py` 신규(7개) — affiliation/stance 저장·기본값,
  `_migrate()` 멱등성, 사람당 최신 1건만 반환, 종목별로 분리되는지, 그리고
  위에서 설명한 행 섞임 버그 회귀 테스트. `tests/test_web.py`에 4개
  추가(시세 없어도 의견은 보임·가격변화는 계산 안 됨, 가격 변화 실제
  계산 확인(2배 가격이면 +100%), 사람당 최신 1건만).
- **검증 방법**: `scripts/add_claim.py`로 실제 테스트 발언 하나를 기록하고
  `build_dashboard_context()` + 진짜 Jinja2 템플릿으로 카드가 제대로
  렌더링되는지 확인(소속·매수 태그·출처 링크·가격 변화 전부 정상 출력).
  확인 후 **테스트 데이터는 바로 DB에서 삭제**(실제 애널리스트 의견인 것
  처럼 남지 않게). `pytest -q` 전체 142 passed(기존 131 + 신규 11:
  test_analyst_views.py 7개 + test_web.py 4개).
- **아직 안 한 것**: `scripts/add_claim.py`로 실제 애널리스트/유튜버 의견을
  몇 건 기록해보는 건 사용자가 직접 할 일(이 세션에서는 지어내지 않음,
  원칙 5). MCP 서버에 이 카드를 툴로 노출하는 것도 아직 안 함(요청 범위가
  대시보드까지였음).

### 완료 (2026-10-06 세션 3차 — 전문가 경험칙 신호: 가중치 기반 위험 점수)

사용자가 "전문가 의견과 경험칙을 가중치 기반으로 합치는 구조"를 요청(전문가 A·
전문가 B·전문가 C 3명, 신호 16개, 백테스트 검증→가중치→위험 점수→대시보드).
**주문 API는 연결하지 않았다**(사용자가 명시적으로 재확인). 범위가 매우 커서
(신규 테이블 5개, 신호 16개, 백테스트/가중치/위험점수 엔진, 대시보드 다수
섹션, 수동입력 CLI, 스케줄 문서, 리포트 생성) 전부 요약하진 않고 핵심만 적는다
— 상세는 README.md의 구조 다이어그램·"알려진 제약" 섹션 참고.

- **DB**: `experts`·`signals`·`signal_values`·`weight_history`·`manual_inputs`
  5개 테이블 신규(`db/schema.sql`). `weight_history`·`manual_inputs`는
  의도적으로 append-only(변경 "이력"이 목적 — 원칙 6의 "중복 행 방지"와는
  다른 성격, db/store.py 주석에 구분 명시). `claims`엔 `source_ref`·
  `date_confidence`('confirmed'|'estimated') 추가.
- **analysis/signals.py**: 16개 신호 순수 함수(전문가 A 6·전문가 B 5·전문가 C 5,
  `YJH_BOX_RANGE`는 BUY/TRIM 2개로 분리, `KSJ_FOREIGN_SELL`은 데이터 없어
  `NotImplementedError`, `KSJ_DCA`는 포맷 규칙이라 on/off 없음). 전부 미래
  참조 금지(causal), 테스트 24개.
- **analysis/signal_backtest.py**: 신호 on일 이후 5/20/63거래일 수익률
  측정, risk=수익률<0 적중/opportunity=수익률>0 적중, 연속 on 구간을 사건
  하나로 묶는 `cluster_events`(중복 표본 방지), 최근 1년 out-of-sample 분리.
  "최대낙폭"은 분포 중 최악의 단일 사례로 재해석(모듈 docstring에 이유 명시
  — 겹치는 forward-window들이라 하나의 자본곡선을 만들 수 없음). 테스트 10개.
- **analysis/weighting.py**: `raw = max(0,적중률-0.5) × n/(n+30)`, 신호
  30%·전문가 40% 상한. **실제 버그를 테스트로 잡음**: 초기 구현은 상한
  초과분을 재배분할 때 재배분 대상의 자기 한도를 안 보고 무한정 부어서
  2신호 케이스에서 15% 상한 신호가 70%까지 치솟는 버그가 있었다 — water-
  filling(한도에 닿은 대상은 빼고 남은 대상에게 반복 재배분)으로 재작성해
  고침. 테스트 13개(이 버그의 정확한 재현 시나리오 포함).
- **analysis/risk_score.py**: 위험 점수 = (켜진 risk 가중치합-opportunity
  가중치합+100)/2, 0~100 클램프. 이 앱은 포트폴리오를 추적하지 않아서
  "보유 중이면" 상태를 별도로 분리 반환(신규매수 보류 여부와 분할매도
  단계를 항상 둘 다 보여줌 — 모르는 보유 여부를 안다고 안 함).
  `KHJ_COMBO==3`이면 기준 점수와 무관하게 분할매도 단계로 덮어씀. 테스트 11개.
- **scripts/**: `seed_signals.py`(experts/signals 정의, 멱등) ·
  `seed_analyst_views.py`(2026-07~08 발언 7건, 1회성) · `add_manual.py`
  (금리단계/마진/PBR/이벤트) · `compute_signals.py`(실데이터로
  signal_values 채움, 멱등) · `recompute_weights.py`(가중치 재계산+
  weight_history 기록) · `signal_scorecard.py`(전체 백테스트 →
  `reports/signal_scorecard.md`). **실제 버그 하나 더 잡음**:
  `recompute_weights.py`가 `weight_cap_pct`가 NULL인 신호를 `x==x` NaN
  체크로 걸러내려 했는데 SQLite NULL이 pandas에서 `None`으로 와서
  `None==None`이 `True`라 통과 못 하고 `caps[sid]=None`이 들어가 TypeError
  로 죽는 버그가 있었다 — `pd.notna()`로 고침. 회귀 테스트 추가.
- **collectors/**: `global_markets.py` 신규(코스닥·S&P500·다우·Euro
  Stoxx50·Sensex·Bovespa·VNM(베트남 ETF 대리)·BIST100·은·IPO ETF·
  ^TNX). `kr_market.py`에 코스피(`^KS11`) 추가.
- **대시보드**: "전문가 경험칙 신호 · 위험 점수" 카드(위험점수·오늘의
  상태·켜진 신호 목록·전문가별 가중치 표), "주시할 이벤트"(날짜 불확실한
  항목, 가중치 없음), "매수 전 체크"(정적 체크리스트, `analysis/
  checklist.py`). 전부 `has_data` 조건 안에서 렌더링, 실제 데이터로
  렌더링 확인(현재 미국 10년물이 4.5%/4.8% 기준 둘 다 넘어서 위험점수
  80점·"분할 매도 2/3 단계" 상태가 실제로 뜨는 것까지 확인).
- **analysis/calendar_rules.py**: `THEMATIC_WATCHLIST` 추가(하이퍼스케일러
  실적 주간·FOMC/금통위·OpenAI·Anthropic 상장·미국 중간선거 — 날짜가
  불확실하거나 반복적이라 기존 날짜-트리거 `events_for()`에 못 넣고 가중치
  없이 표시만 하는 별도 리스트로 분리).
- **실제 실행 결과**(000660, 2005-01-01~): `python scripts/compute_signals.py`
  로 11/16개 신호 계산 성공(나머지 5개는 수동입력 미입력 3개·KIS 미구현
  1개·포맷규칙 1개로 전부 "건너뜀" 사유가 명확함). `python scripts/
  recompute_weights.py` 결과 `YJH_US10Y`(55.9%)·`KSJ_US10Y_HIGH`(60.1%)·
  `KHJ_IPO_WEAK`(50.8%)만 적중률 50% 초과라 가중치를 받았고(각 30%, 상한
  도달) 나머지 11개는 가중치 0% — "동전던지기 이하 신호는 가중치 0"이
  실제로 그렇게 동작함을 확인. `python scripts/signal_scorecard.py`로
  `reports/signal_scorecard.md` 생성(211줄) + `KHJ_3DAY_SELLOFF` 임계값
  민감도(-2/-3/-5%) 비교까지 포함.
- `pytest -q` **212 passed**(이번 세션 전 142 → 70개 신규: signals 25,
  signal_backtest 10, weighting 13, risk_score 11, manual_inputs 5,
  recompute_weights 2, checklist/watchlist 4).
- **아직 안 한 것**:
  - `KHJ_RATE_STAGE`·`YJH_MEMORY_MARGIN`·`YJH_PBR_FLOOR`는 사용자가
    `scripts/add_manual.py`로 실제 값을 입력해야 계산된다 — 이 세션에서
    지어내지 않았다(원칙 5와 같은 태도를 수치에도 적용).
  - `KSJ_FOREIGN_SELL`은 KIS `investor_flow()` 구현 후 `enabled=1`로
    바꿀 것(가중치 상한 15%는 이미 설정됨).
  - `collectors/collect_daily.py`에 `global_markets.py`를 안 묶었다(기존
    테스트의 하드코딩된 job 3개 assertion을 건드려야 해서 이번 범위 밖으로
    뺌) — 지금은 `python collectors/global_markets.py`를 따로 돌려야 한다.
  - 매주 일요일 자동 재계산은 README에 cron/Windows 작업 스케줄러 등록
    방법만 적어뒀고, 실제로 스케줄을 등록해보진 않았다(이 세션 환경에
    Windows가 없음).
  - `KSJ_BREADTH`(반도체 쏠림)는 KOSPI 상승종목비율 데이터가 없어 반도체
    2종목 가중평균으로 근사했다 — 정확한 지표가 아니다(README에 명시).
  - 위험 점수 전환 기준(60/80점)·가중치 상한(30%/40%)은 사용자가 지정한
    시작 기본값이며 백테스트로 최적화하지 않았다.

### 완료 (2026-10-06 세션 4차, 1단계 — 신호 적중률 통계 검증)

사용자가 "다음 개발"을 6단계로 요청(API 키 없이, KIS/Anthropic 실제 호출
없이). 1단계(최우선)부터 순서대로, 단계마다 pytest 통과 확인 후 진행.

- `analysis/signal_backtest.py`: `base_rate()`(그 구간 "아무 날이나 찍었을
  때"의 비율, 0.5 아님) · `binomial_p_value()`(이벤트 단위, one-sided
  "기준률보다 높다"만 검정 — 음의 edge는 애초에 가중치 0이라 검정 불필요) ·
  `bootstrap_ci()`(이벤트 단위 복원추출 2000회, 95% CI — 사용자가 제시한
  "이벤트 묶음 또는 블록 부트스트랩" 중 이벤트 묶음 쪽 선택, 사유는 모듈
  docstring에) · `benjamini_hochberg()`+`apply_benjamini_hochberg()`(다중비교
  보정, q-value). `SignalPerformance`에 `base_rate_pct`·`edge_pct`·
  `p_value`·`ci_lower_pct`·`ci_upper_pct`·`q_value`·`significant` 필드
  추가(기존 필드는 안 건드림, 기존 테스트 10개 그대로 통과). scipy 신규
  의존성 추가(binomtest용, `requirements.txt`).
- `analysis/weighting.py`: **구버전(`compute_weights`/`raw_score`, 50% 기준)은
  그대로 남기고**(기존 테스트 13개 안 건드림) `raw_score_significant()`·
  `compute_weights_significant()` 신규 — 신뢰구간 하한이 기준률 이하거나
  이벤트 수가 min_events(기본 30) 미만이면 가중치 0. 캡핑 로직은
  `_distribute_and_cap()`으로 공통화해서 두 버전이 같은 water-filling을
  공유. **사용자가 확인을 요청한 KHJ_IPO_WEAK(적중률 50.8%)는 실제로
  가중치 0이 됨을 확인** — edge는 양수(+0.8%p 수준)였지만 신뢰구간이 넓어
  하한이 기준률 밑으로 내려갔다.
- `scripts/recompute_weights.py` 전면 교체: **walk-forward 검증** — 가중치는
  in-sample(최근 1년 이전) 성적만으로 산정하고, out-of-sample(최근 1년)
  적중률은 비교 표시만(가중치 계산에는 안 씀). n(이벤트)·적중률·기준률·
  CI하한·q값·OOS n/적중률·이전/신규 가중치를 한 표로 출력. **위험 점수
  변경 전/후 비교**도 출력(오늘 켜진 신호 기준, 이전 가중치 vs 신규 가중치).
  `--min-events` 옵션 추가.
- `scripts/signal_scorecard.py`: 신호×기간(5/20/63일)별 기준률·95%CI·p값·
  q값 컬럼 추가. **다중비교 보정은 같은 구간(전체/검증제외/최근1년) 안의
  모든 신호×기간을 한 묶음으로** 본다(구간이 다르면 다른 데이터라 따로
  보정). "유의" 표시는 가중치를 받는 조건과 동일(CI하한>기준률 and
  이벤트수≥최소기준).
- `web/main.py`·`index.html`: 대시보드 신호 표에 이벤트수·기준률·95%CI·
  q값·최근1년(OOS) 적중률 컬럼과 "유의" 태그 추가. q_value는 대시보드에
  보이는 신호 전체를 한 묶음으로 BH 보정(제자리 수정이라 순서 주의 —
  on_signals 딕셔너리를 만들기 **전에** `apply_benjamini_hochberg`를 먼저
  실행해야 한다, 처음에 반대로 짜서 NaN이 그대로 찍히는 버그를 직접
  재현하고 고쳤음).
- 테스트 55개 신규(test_signal_backtest_stats.py 16, test_weighting_
  significant.py 10, test_recompute_weights_walkforward.py 4 등) — "무작위
  신호는 가중치 0"을 포함.
- **실제 발견한 중요한 결과**: 기존(2026-10-06 3차 세션) 가중치는 50% 기준
  고정 비교로 `YJH_US10Y`·`KSJ_US10Y_HIGH`·`KHJ_IPO_WEAK`에 30%씩(총 90%)을
  줘서 위험점수 80점("분할 매도 2/3 단계")이 나왔는데, **기준률 대비
  신뢰구간으로 다시 보니 14개 신호 중 통계적으로 유의한 신호가 0개**였다
  (전부 신뢰구간 하한이 기준률 밑). 가중치가 전부 동일배분(초기값)으로
  떨어지면서 위험점수도 **57.5점("매수 가능")으로 바뀜** — 이전 결과가
  통계적으로 근거 없었다는 걸 숫자로 보여준 것. `pytest -q` 242 passed.

### 완료 (2026-10-06 세션 4차, 2단계 — 백테스트 기대값 검산 + 거래비용 파라미터화)

1단계에 이어 2단계 진행. 요청한 "기대값 검산(median을 승리 크기로 잘못 쓰는
버그 찾기)"은 **버그 없음으로 결론**났고, 나머지(거래비용 파라미터화 +
비용 전/후 나란히 출력 + -99% 분해)는 전부 구현했다.

- **기대값 검산**: `grep -rn "median"`으로 analysis/·scripts/ 전체 확인 —
  median은 항상 mean과 **별도의 설명용 통계**로만 표시되고(예:
  `regime_breakdown()`의 중앙값 컬럼), 기대값(`mean_return`) 계산에
  섞여 들어가는 곳은 없었다. `tests/test_expectancy_reconciliation.py`
  신규(8개) — `mean_return == 승률×평균수익 - 패율×평균손실`이 다양한
  거래 리스트(전부 승리/전부 패배/동률/큰 꼬리손실 포함)에서 수학적으로
  항상 일치함을 검증. **코드 변경 없음, 검산만** — 테스트 파일 docstring에
  그대로 명시.
- **거래비용 파라미터화** (`analysis/trading_costs.py` 신규):
  - `tax_rate_on(날짜, schedule)` — 날짜별 매도세율 조회. **세율 변경
    이력은 실제로 확인된 값이 하나도 없어서(원칙: 값을 지어내지 않음)
    `TAX_RATE_SCHEDULE`엔 이 프로젝트 기존 기본값(0.20%) 1건만 넣고
    TODO로 "사용자가 확인된 이력으로 교체할 것"을 모듈 docstring에
    명시** — 과거 연도별 실제 세율 변경 시점을 지어내지 않았다.
  - `DEFAULT_SLIPPAGE_RATE = 0.0`(기존 동작 그대로, 옵트인).
  - `analysis/pullback.py`·`analysis/price_rule_backtest.py`의
    `simulate_*_trades()`에 `slippage_rate`·`tax_rate_schedule` 매개변수
    추가(기본값은 기존 동작과 100% 동일 — 하위호환). `tax_rate_schedule`을
    주면 **청산일(매도일) 기준**으로 날짜별 세율을 찾아 쓰고, 안 주면
    기존처럼 고정 `tax_rate`를 쓴다.
  - `tests/test_trading_costs.py`(5개)·`test_trading_costs_wiring.py`
    (5개) — 날짜 조회 로직 + 두 백테스트 엔진에 실제로 slippage/세율
    스케줄이 수익률에 반영되는지 손계산 가능한 작은 거래로 회귀 테스트.
- **비용 반영 전/후 나란히 출력**:
  - `scripts/backtest_strategies.py`의 `--strategy pullback`에 세 번째
    변형 `"pullback (손절 3.0%, 비용 0)"` 추가 — 기존 `_print_table`이
    총수익률/연환산/MDD/n/승률/평균수익/평균손실/최악거래/노출도를 전부
    가진 표라 새 출력 코드 없이 바로 비용 전/후가 나란히 보인다(전체/
    in-sample/out-of-sample 세 구간 모두). 실제 000660 실행 결과: 비용
    포함 평균수익/평균손실 +1.77%/-3.22% vs 비용 0 +2.00%/-3.00% — 거래
    수(1139건)·승률(57.2%)·노출도는 비용과 무관하게 동일(거래 목록 자체는
    체결가 조건으로만 정해지고 비용은 체결가에만 곱해지는 값이라서).
  - `scripts/decompose_pullback.py` 신규 — pullback 기본 파라미터가 왜
    거의 -99%로 수렴하는지 세 원인으로 분해(코드 변경 없이 측정만):
    1. **거래비용 효과**: 복리 총수익률 비용포함 -98.99% vs 비용0 -86.11%
       → 비용 효과 **-12.89%p**.
    2. **복리(compounding) vs 단순합산**: 단순합산 -423.67% vs 복리
       -98.99% → 차이 **+324.68%p**(복리가 "원인"이 아니라, 단순합산이
       한도 없이 음수로 뻗어나가는 **오해를 부르는 비교 기준**이라는 뜻 —
       복리는 -100%가 하한이라 단순합산보다 덜 나쪽처럼 보이는 것뿐).
    3. **최악 손실 거래 10건 제외**: 제외해도 복리 결과가 **+0.39%p**만
       바뀜 — 무시할 수준.
    - **결론(숫자로 확인, 세 후보 중 가장 큰 차이가 주원인)**: 비용 0
      상태에서도 -86% 손실이 나므로, 이 전략은 **비용과 무관하게 이미
      음의 기대값**(거래비용 섹션의 손계산: 0.572×1.77% - 0.428×3.22%
      ≈ -0.36%/거래)을 1139건 거치며 누적한 결과다. 비용은 결과를
      악화시키지만(-12.89%p) 주된 원인이 아니고, 소수의 꼬리 손실 거래가
      전체를 끌어내린 것도 아니다(-99%는 "몇 번의 큰 손실"이 아니라
      "꾸준히 작게 지는 다수의 거래"가 누적복리로 만든 결과) — 직전
      세션(2026-10-05 4차)에서 손으로 추정한 결론과 일치, 이번엔 숫자로
      분해해 재확인.
- `pytest -q` **260 passed**(1단계 종료 시 242 → 신규 18:
  test_expectancy_reconciliation.py 8, test_trading_costs.py 5,
  test_trading_costs_wiring.py 5).
- **아직 안 한 것**: `TAX_RATE_SCHEDULE`은 여전히 기본값 1건뿐이다 — 실제
  과거 매도세율 변경 이력(연도별)은 사용자가 공식 출처로 확인 후
  `analysis/trading_costs.py`에 직접 추가해야 한다(지어내지 않기로 한
  원칙 때문에 이 세션에서 비워둠). `decompose_pullback.py`는 pullback
  전략 전용이고, price_rules 쪽은 아직 같은 분해 스크립트가 없다(필요하면
  같은 패턴으로 추가 가능).

### 완료 (2026-10-06 세션 4차, 3단계 — 수집 진입점 통합)

- **`collectors/collect_daily.py`에 `global_markets.py` 추가**: 직전 세션
  CLAUDE.md "아직 안 한 것"에 적혀 있던 항목("collect_daily에
  global_markets가 안 묶여 있음")을 해결 — 이제 us_market·kr_market·
  global_markets·news **4개**를 이 순서로 순차 실행한다. **사용자가 이번에
  나열한 목록(kr_market·global_markets·news)에는 us_market이 없었지만,
  이미 동작하던 수집기를 근거 없이 빼는 건 공통 규칙("기존 테스트를
  삭제해서 통과시키지 마")의 취지에 맞지 않다고 판단해 그대로 유지하고
  global_markets만 추가했다** — 이 판단은 CLAUDE.md에 명시해 다음에 확인
  가능하게 함.
  - `--only us,kr,global,news`(쉼표 구분, 짧은 이름)로 일부만 선택 실행
    가능 — 순서는 지정한 순서를 그대로 따른다. 알 수 없는 이름을 주면
    `ValueError`(CLI에서는 `argparse.error`로 종료 코드 2).
  - `--dry-run` — 아무 수집기도 호출하지 않고 collect_log도 안 남기고
    실행될 job 목록만 출력(네트워크·DB 접근 전혀 없음).
  - `collect_daily(period, only, dry_run)` 함수형 API로 재작성해서
    `scripts/run_weekly.py`가 그대로 import해서 테스트 가능.
- **기존 테스트(`tests/test_collect_daily.py`) 수정**: 하드코딩돼 있던
  "3개 job" 전제를 "4개 job"으로 바꾸고(모든 테스트에 `global_markets`
  monkeypatch 추가) 검증 의도(순차 실행·독립 실패·로그 기록·종료 코드)는
  그대로 유지했다 — 파일 상단 docstring에 무엇을 왜 바꿨는지 설명 추가.
  `--only`·`--dry-run`·멱등 쓰기(같은 데이터로 두 번 실행해도 `daily_price`
  행이 안 늘어나는지 — `collect_log`는 누적이 설계 의도라 늘어나는 게
  정상이라는 점도 별도 테스트로 구분) 테스트를 새로 추가, 총 16개
  (기존 7 + 신규 9).
- **`scripts/run_weekly.py` 신규**: collect_daily → compute_signals →
  recompute_weights를 이 순서로 체이닝. 세 스크립트를 import해서 로직을
  합치지 않고 **서브프로세스로 순서대로 호출**한다(각 스크립트가 이미
  완성된 독립 CLI고, "CLI는 얇은 배선"이라는 이 프로젝트 기존 관행과
  일치, `recompute_weights.py`는 애초에 재사용 가능한 함수로 안 쪼개져
  있어서 import 방식이 더 복잡해짐). `collect_daily.py`와 달리 **한
  단계가 실패하면 체인을 중단**한다 — 세 단계는 앞 단계의 출력(시세 →
  신호값 → 가중치)에 의존하는 종속 관계라서, 수집기들끼리의 "서로 독립"
  원칙과는 다른 판단. `--period`·`--only`(collect_daily에 전달)·
  `--min-events`(recompute_weights에 전달)·`--dry-run`(아무 서브프로세스도
  안 부르고 실행될 명령 3개만 출력) 지원.
  `tests/test_run_weekly.py` 신규(9개) — `subprocess.run`을 전부
  monkeypatch로 대체해서 실제 프로세스를 띄우지 않고 명령 구성·순서·
  중단 동작·dry-run만 검증.
- **README.md**: `collect_daily.py` 사용법(4-job, `--only`, `--dry-run`)·
  구조 다이어그램(global_markets 포함, run_weekly.py 추가) 갱신.
  **Windows 작업 스케줄러 등록은 예시로만 문서화**했고(요청대로),
  이 세션에서 실제로 등록을 수행하지는 않았다 — 기존 "compute_signals.py
  && recompute_weights.py를 `&&`로 못 이어서 .bat 파일이 필요하다"는
  설명을 `run_weekly.py` 하나로 대체해 더 간단해졌다는 점도 반영.
- `pytest -q` **279 passed**(2단계 종료 시 260 → 신규 19:
  test_collect_daily.py 9개 추가(기존 7 전부 수정), test_run_weekly.py 9개).
- **아직 안 한 것**: 실제로 cron/작업 스케줄러에 등록해보는 것(요청대로
  문서화만, 등록 자체는 하지 않음). `run_weekly.py`를 실제 네트워크
  환경에서 통째로 한 번 돌려보는 것(이 세션은 `--dry-run`과 단위
  테스트로만 검증, 세 하위 스크립트 각자는 이전 세션들에서 이미 실제
  데이터로 검증됨).

### 완료 (2026-10-06 세션 4차, 4단계 — KIS investor_flow 키 없이 미리 구현)

**실제 KIS API는 한 번도 호출하지 않았다**(공통 규칙·CLAUDE.md 보안 원칙
그대로). 응답 파싱 로직을 가짜 fixture로 미리 짜두고, 실제 키가 생기면
`collectors/kis_client.py` 상단의 TODO만 공식 문서로 재확인하면 되는
상태로 만들었다.

- **`collectors/kis_client.py`**: `investor_flow(code)` 구현 — tr_id
  (`FHKST01010900`)·엔드포인트(`/uapi/.../inquire-investor`)·응답 필드명
  (`prsn_ntby_qty`/`frgn_ntby_qty`/`orgn_ntby_qty`)은 전부 **공식 문서
  재확인 전 최선의 추정**이라고 모듈 상단에 명시하고 각 줄에 TODO를
  남겼다 — 이 세션엔 실제로 호출해본 적이 없어서 틀릴 수 있다는 걸
  숨기지 않았다(원칙: 지어내지 않기). `parse_investor_flow_response(raw,
  ticker)`를 네트워크 호출과 분리된 순수 함수로 빼서 fixture만으로 검증
  가능하게 함 — 필드가 없거나 숫자로 안 바뀌면 0이 아니라 **None**으로
  두고(값 지어내지 않음), 날짜 필드가 없는 행은 날짜를 지어낼 수 없으니
  건너뛴다.
  - `collect_investor_flow(code, paper)` 신규 — 다른 `collectors/*.py`의
    `collect_all()` 패턴과 동일한 얇은 배선. `settings.kis_ready`가
    False(키 없음)면 **네트워크 시도 자체를 안 하고** 0을 반환한다.
    `collect_daily.py`의 job으로는 **아직 안 묶었다** — 실제 키로 한
    번도 검증 못 한 코드를 매일 배치에 자동으로 태우는 건 더 신중해야
    한다고 판단(아래 "아직 안 한 것" 참고).
  - **보안**: `KisClient.__repr__`을 새로 정의해서 기본 repr이 `__dict__`
    를 그대로 노출하는 걸 막음(`print(client)`나 traceback에 앱키/시크릿/
    캐시된 토큰 값이 찍히는 사고 방지 — `key_set`/`secret_set` 불리언만
    보여줌). 조회 파라미터(`params`)에는 애초에 앱키/시크릿이 안 들어가고
    헤더(`_headers()`)에만 있다는 것도 테스트로 재확인. `.kis_token.json`
    이 `.gitignore`에 이미 있는 것도 재확인(기존 상태 그대로, 변경 없음).
    주문(매수/매도) 관련 메서드가 없는지 메서드 이름 목록으로 검증하는
    테스트도 추가(원칙 2 재확인).
  - `tests/fixtures/kis_investor_flow_response.json`·
    `..._missing_fields.json` 신규 — 둘 다 파일 안에 "실제 응답 아님,
    손으로 만든 테스트용 가짜 데이터"라고 명시. `tests/test_kis_client.py`
    신규(17개, 1개는 `KIS_LIVE_TEST=1`일 때만 도는 실네트워크 테스트라
    기본 skip) — 파싱/정규화/방어적 처리·멱등 저장(`store.upsert_flow`
    재실행해도 행 안 늘어남, 값이 바뀌면 덮어써지는지)·키 없을 때 호출
    안 됨·비밀값 미노출 전부 검증.
- **`analysis/signals.py`의 `ksj_foreign_sell()`**: 이전엔 항상
  `NotImplementedError`였는데, 실제 계산 함수로 교체 — `foreign_net_qty`
  (외국인 순매수 하루치 Series)를 받아 20일 누적 순매도(≤0)면 다음 날
  on. 단위(수량 vs 금액)는 investor_flow()와 마찬가지로 미확인이라 함수
  자체는 단위에 무관하게 부호·합산만 쓰도록 짰다. 빈 입력이면 빈 결과를
  그대로 반환(지어내지 않음). `tests/test_signals.py`의 기존
  `TestKsjForeignSellNotImplemented`(raises 테스트 1개)를 실제 동작 검증
  4개로 교체(순매도 지속 시 on·순매수 시 off·미래 참조 없음·빈 입력 처리).
- **`db/store.py`**: `get_foreigner_net(ticker)` 신규 — `get_flow()`는
  대시보드용으로 최근 N일만 내림차순으로 주는 구조라 rolling 계산에 안
  맞아서(기간이 짧으면 rolling 결과가 전부 NaN), `get_volume()`과 같은
  패턴으로 전체 기간 오름차순 Series를 따로 뺐다.
- **`scripts/compute_signals.py`**: `KSJ_FOREIGN_SELL`을 무조건
  `skip()`하던 자리를 `store.get_foreigner_net(HYNIX)`가 비어있는지로
  분기하도록 교체 — 데이터 있으면 계산, 없으면("KIS 키 필요") 건너뛴다.
  지금은 investor_flow 테이블이 비어 있으니(이 세션에서 실제 수집 안 함)
  여전히 "데이터 없음"으로 건너뛰지만, 코드 경로 자체는 완성됐다.
- **`scripts/seed_signals.py`**: `KSJ_FOREIGN_SELL`의 `enabled`를 0→1로
  전환(구현 완료했으니 "구현 후 enabled=1로 전환할 것"이라는 기존 TODO
  주석 그대로 실행) — 실제 데이터가 없으면 `recompute_weights.py`가 이미
  갖고 있는 "이벤트 0건 → 가중치 0" 처리(1단계에서 만든 더미
  `SignalPerformance(n_events=0)` 경로)로 자연스럽게 가중치 0이 되고
  크래시하지 않는다. description에 이번에 구현 완료했다는 사실과 아직
  실데이터는 없다는 점을 둘 다 남김.
- `pytest -q` **298 passed, 1 skipped**(3단계 종료 시 279 → 신규 20:
  test_kis_client.py 17개(16 pass + 1 skip)·test_signals.py 순증 3개(1개
  삭제+4개 추가)).
- **아직 안 한 것**:
  - `collect_investor_flow()`를 `collect_daily.py`의 job으로 묶는 것 —
    이번엔 일부러 안 묶었다. 실제 키로 한 번도 호출해본 적 없는 코드를
    자동 배치에 바로 태우면, 필드명 추정이 틀렸을 때(TODO 참고) 매일
    조용히 잘못된 값을 쌓을 위험이 있다 — **KIS 키 발급 후 먼저
    `python collectors/kis_client.py`(또는 별도 수동 스크립트)로 raw
    응답을 한 번 직접 확인하고, TODO의 필드명이 맞는지 검증한 다음에
    `collect_daily.py`에 추가하는 걸 권장**.
  - `KIS_APP_KEY`/`KIS_APP_SECRET`는 여전히 `.env`에 없다(모의투자 계좌로
    발급 예정, 이 세션에서도 발급하지 않음).
  - tr_id·엔드포인트·필드명 추정이 맞는지는 실제 키로 호출해봐야 확정된다
    — 지금은 "모양은 맞게 짜뒀지만 숫자가 실제로 맞는지는 모른다"는
    상태.

### 완료 (2026-10-06 세션 4차, 5단계 — 위험점수 설명가능성 + 원칙 정합성)

1단계가 끝나 있어야 진행하는 단계라고 사용자가 못 박은 항목. 설명가능성
4개(점수 분해·쏠림 경고·수동입력 최신성·과거 점수 재구성)를 전부 구현하고,
마지막 "원칙 정합성" 질문은 코드를 먼저 바꾸지 않고 사용자에게 두 선택지로
물어서 **"CLAUDE.md 원칙을 갱신"**으로 확정받았다.

- **점수 분해(`analysis/risk_score.py`의 `score_contributions()`)**: 켜진
  신호 각각이 위험 점수에 얼마나 기여했는지 — risk면 +weight_pct/2,
  opportunity면 -weight_pct/2(50점 중립 기준, `compute_risk_score()` 공식을
  그대로 풀어서 유도). `RiskState`에 `contributions` 필드로 추가, 대시보드
  (`web/main.py`·`index.html`)에 가로 막대 바로 표시(막대 길이는 |기여도|,
  risk는 빨강/opportunity는 초록).
- **쏠림 경고(`concentration_warnings()`)**: 같은 원천 지표에서 파생된
  신호가 동시에 켜지면 가중치가 이중으로 잡힌 것처럼 보일 수 있다는 경고.
  `UNDERLYING_INDICATOR_GROUPS` 딕셔너리로 그룹을 **수동 지정**한다(상관계수
  자동 탐지 아님 — "같은 원천"은 상관이 아니라 설계 지식이라고 판단, 지금은
  사용자가 예로 든 US10Y(^TNX) 하나: `YJH_US10Y`+`KSJ_US10Y_HIGH`). 그룹
  내 켜진 신호가 2개 이상이고 그 가중치 합이 켜진 전체 가중치 합의
  60%(`DEFAULT_CONCENTRATION_RATIO`, 시작 기본값)를 넘으면 경고. 실제 DB로
  렌더링해보니 **지금 이 두 신호가 둘 다 켜져 있어서 실제로 경고가 뜬다**
  (대시보드에서 직접 확인) — 가상 시나리오가 아니라 현재 상태에서 바로
  의미 있는 경고.
- **수동 입력 최신성(`db/store.py`의 `manual_input_freshness()`)**:
  `rate_stage`/`memory_margin`/`pbr` 각각이 as_of 시점 기준 며칠 지났는지와
  `stale_days`(기본 90일) 초과 여부. **점수에서 자동으로 빼지는 않는다** —
  "포함 여부는 옵션"이라는 요청 문구를 "기본은 포함, 경고만 표시"로 해석해
  관찰만 하고 멈추는 쪽을 기본값으로 선택(자동 제외는 더 큰 행동 변화라 이
  세션에서 임의로 켜지 않음). 대시보드에 "수동 입력 오래됨" 경고로 표시.
  입력 자체가 없으면 "없음"(exists=False)으로 — 지어내지 않음.
- **과거 위험 점수 시계열 재구성(`analysis/risk_score_history.py` 신규,
  순수 함수)**: `weights_as_of()`가 날짜별로 "그 날짜까지 기록된 가장
  최근 가중치"만 쓴다(`weight_history`, merge_asof backward — 미래 참조
  금지). 가중치 기록이 아예 없는 냉시작 구간은 `effective_weights_as_of()`
  가 균등 가중치로 채우되 `cold_start` 플래그로 구분해서 **숨기지 않는다**.
  `reconstruct_score_series()`로 날짜별 점수를 벡터화 계산,
  `bucket_forward_returns()`로 점수 구간×호라이즌(5/20/63일)별 n·평균·
  중앙값·최대손실을 낸다(원칙 3·4 그대로). `scripts/
  risk_score_history_report.py` CLI 추가.
  - **실제 실행 결과(000660, 2000~2026, 6686거래일)**: `weight_history`가
    실제로는 **단 한 번**(2026-10-06, 가격 데이터 마지막 날짜 2026-10-02
    이후)만 기록돼 있어서, **가격 히스토리 전체(100%)가 냉시작 구간**으로
    나왔다 — 버그가 아니라 "이 시스템이 아직 실제 백테스트로 가중치를
    여러 번 재계산해본 적이 없다"는 사실을 숫자로 보여준 것(1단계·2단계와
    같은 패턴: 솔직한 한계를 찾아서 숨기지 않고 기록). 균등 가중치 상태의
    점수는 40~80 구간에만 분포했고(0~40·80~100은 표본 0, 균등 가중치로는
    그 극단에 못 닿음) 40~60/60~80 구간 모두 20·63일 평균 수익률이 양수로
    나왔으나, **n<30인 구간도 섞여 있어 그대로 해석하면 안 됨**(출력에
    경고 표시). weight_history가 여러 번 쌓이는 미래 시점에 이 리포트를
    다시 돌리면 더 의미 있어질 것.
- **원칙 정합성 질문**: "분할 매도 2/3 단계" 문구가 원칙 1·2(예측·매매
  미구현)와 충돌할 수 있다는 점을 사용자에게 두 선택지로 제시 — (1) 관찰형
  문구로 교체 (2) CLAUDE.md 원칙 갱신. **사용자가 (2)를 선택**해서, 원칙
  1 섹션에 "위험 점수 상태 문구도 매수/목표/손절가 노출 예외와 같은 논리로
  허용한다(기계적 계산 + 근거 항상 동반 표시, LLM 서술에는 여전히 적용 안
  됨)"는 조항을 추가했다. **코드(문구 자체)는 바꾸지 않았다** — 사용자가
  선택한 옵션 자체가 "문구는 그대로, 원칙만 갱신"이었음.
- `pytest -q` **335 passed, 1 skipped**(4단계 종료 시 298 → 신규 37:
  test_risk_score.py 신규 11개, test_risk_score_history.py 12개,
  test_manual_inputs.py 신규 6개, test_web.py 신규 3개,
  test_risk_score_history_report.py 5개).
- **아직 안 한 것**:
  - 수동 입력 최신성을 "점수에서 자동 제외"하는 옵션은 기본값을 켜지
    않았다 — 켜고 싶으면 `web/main.py`에서 `on_signals`를 `build_risk_state()`
    에 넘기기 전에 stale한 입력에 의존하는 신호(KHJ_RATE_STAGE 등)를
    걸러내는 필터를 추가하면 된다(아직 안 만듦, 원한다면 다음에).
  - `UNDERLYING_INDICATOR_GROUPS`는 지금 US10Y 하나뿐이다. 다른 신호 간
    상관(예: KHJ_CONCENTRATION이 여러 시장을 묶는 방식)도 같은 식으로
    그룹을 늘릴 수 있음 — 이번엔 사용자가 예로 든 것만 반영.
  - 과거 점수 재구성은 weight_history가 더 쌓여야(매주 recompute_weights.py
    실행이 누적돼야) 진짜 의미 있는 결과가 나온다 — 지금은 인프라만 완성.

### 완료 (2026-10-06 세션 4차, 6단계 — 데이터 정합성 정리)

6단계(마지막 단계)까지 완료. 전부 "비교/스캔만 하고 자동 보정하지 않는다"
는 공통 규칙을 지켰다 — 불일치를 찾아도 DB 값을 코드가 알아서 고치지
않는다.

- **`scripts/verify_outlier.py` 신규**: `data/manual/`(신규 폴더, README로
  CSV 형식 안내)에 사용자가 직접 넣은 KRX 공식 종가 CSV와 DB(yfinance
  기반) 값을 대조한다. 기본 대상은 2026-10-05 세션에서 발견한 000660
  2001-11-12(+32.08%) 이상치(지어낸 값 아님 — 이전 세션의 실제 발견을
  그대로 참조). `--ticker`/`--date`로 임의 날짜도 대조 가능. CSV 열 이름은
  `date`/`일자`/`날짜`·`close`/`종가` 중 하나씩만 찾으면 되게 유연하게
  파싱하고, 못 찾으면 빈 결과(지어내지 않음). **실제로 돌려보니 아직
  data/manual/에 공식 CSV가 없어서 "비교 불가"로 나온다** — DB 쪽 값
  (+32.08%)은 재확인됐지만, 공식 데이터와의 대조는 사용자가 CSV를 직접
  넣어야 가능한 상태로 남아 있다.
  - `data/manual/README.md` 신규(CSV 형식 안내), `.gitignore`에
    `data/manual/*.csv` 추가(KRX 원본 데이터를 커밋하지 않음, README만 추적).
- **가격제한 스캔(`analysis/price_limits.py` 신규 + `verify_outlier.py
  --scan-limits`/`--scan-limits-all`)**: 날짜별 KRX 가격제한(상하한 %)
  테이블을 만들어 전체 수익률을 대조한다. **analysis/trading_costs.py의
  세율 스케줄과 같은 이유로 한 건만 확신 있게 넣었다** — 2015-06-15
  ±30% 개편(비교적 널리 알려진 날짜). 그 이전(6%/8%/12%/15% 단계로
  바뀌어온 것으로 알려져 있으나 정확한 시행일을 이 세션에서 공식 출처로
  확인 못함)은 비워두고 TODO로 남겼다 — 그 구간 날짜는 "위반 아님"이
  아니라 "판정 불가"로 건너뛴다(모르는 걸 위반이라고도 정상이라고도
  안 함). `db/store.py`에 `list_tickers()` 추가.
  - 기본 스캔 대상은 `KRX_EQUITY_TICKERS`(000660·005930, 실제 가격제한이
    적용되는 KRX 개별종목만)로 좁혔다 — **처음에 DB의 모든 티커로
    돌려봤다가 US10Y(미국 10년물 금리)·SILVER(은 선물)가 "위반"으로
    잘못 걸리는 걸 실제로 확인**하고(둘 다 가격제한 대상이 아님),
    기본 범위를 좁히는 쪽으로 고쳤다(`--scan-limits-all`로 여전히 전체
    스캔 가능, 오탐 주의 문구와 함께).
  - 실제 DB(000660·005930, 2000~2026)로 돌려본 결과: 2015-06-15 이후
    구간에서 가격제한 초과 **0건**(판정 가능한 범위 안에서는 깨끗함).
- **ADR 전환비율(`analysis/adr.py`)**: 기존에도 코드 주석으로 "공식
  예탁계약서 미확인"이 있었지만, 이번에 (1) `config.py`에 `adr_ratio`
  설정(`.env`의 `ADR_RATIO`로 override, 빈 문자열도 기본값 10으로
  안전 처리) 추가, (2) `web/main.py`가 `adr.premium()` 호출 시 이
  설정을 쓰도록 연결, (3) 대시보드 "ADR 프리미엄 상세" 카드에 미확인
  상태 안내 문구를 직접 표시하도록 템플릿 추가 — 전부 요청대로 "주석+
  대시보드 표시 + config override" 3개를 채웠다. **analysis/adr.py에는
  지금까지 테스트가 전혀 없었다**는 것도 이번에 발견해서 `tests/
  test_adr.py` 신규(7개)로 기본 커버리지를 채워 넣었다(이번 범위 밖
  기존 공백이었지만, 건드리는 모듈이라 같이 고쳤다).
- **`KSJ_BREADTH` "근사 지표" 배지**: `web/main.py`에 `APPROXIMATE_SIGNALS
  = {"KSJ_BREADTH"}` 상수 추가, 신호가 켜졌을 때 `on_signals[sid]
  ["approximate"]`로 표시하고 대시보드 신호 테이블에 경고색 배지로
  노출(`index.html`). KOSPI 상승종목비율 데이터가 없어 반도체 2종목
  가중평균으로 근사한다는 기존 설명(data_source 주석)은 그대로 두고,
  화면에서도 "유의" 배지와 구분되는 별도 배지로 바로 보이게 했다.
- `.env.example`에 `KIS_APP_KEY`/`KIS_APP_SECRET`/`KIS_ACCOUNT`/
  `KIS_USE_PAPER`/`ADR_RATIO`가 빈 값으로 누락돼 있던 걸 발견해서 같이
  채워 넣었다(CLAUDE.md 보안 원칙이 이미 "`.env.example`에는 변수
  이름과 빈 값만" 요구하고 있었는데 이 네 개 KIS 변수가 빠져 있었음 —
  이번에 손대는 김에 같이 고침, 값은 전부 비움).
- `pytest -q` **374 passed, 1 skipped**(5단계 종료 시 335 → 신규 39:
  test_adr.py 7개, test_price_limits.py 10개, test_verify_outlier.py
  19개, test_web.py 신규 3개(ADR override 1 + 근사 배지 2)).
- **아직 안 한 것**:
  - `data/manual/`에 실제 KRX 공식 CSV를 아직 아무도 넣지 않았다 — 000660
    2001-11-12 이상치의 원인을 실제로 밝히려면 사용자가 KRX 정보데이터
    시스템 등에서 직접 받은 CSV를 넣고 `python scripts/verify_outlier.py`
    를 다시 돌려야 한다.
  - 가격제한 1996/1998 전후 구간별 정확한 시행일은 여전히 미확인 —
    확인되면 `analysis/price_limits.py`의 `PRICE_LIMIT_SCHEDULE`에
    `PriceLimitChange`를 추가할 것(코드 구조는 이미 여러 구간을 받게
    돼 있음).
  - ADR 전환비율 1:10도 공식 예탁계약서로는 여전히 재확인 안 됨 — 이번엔
    "확인 전까지 참고용"이라는 걸 더 눈에 띄게(대시보드+config) 만든
    것뿐, 숫자 자체가 맞는지는 그대로 미확인.

### 완료 (2026-10-07 세션 — DART(전자공시) 연동, 키 없이 구현)

사용자가 DART 연동을 요청(5개 섹션: 클라이언트·DB·신호 연결·공시→claims
시딩·수집 진입점). **이 세션은 DART API 키 없이 진행했고, 실제 호출은
한 번도 하지 않았다** — KIS investor_flow() 때(4단계, 2026-10-06)와 같은
패턴: 공식 문서 기준 best-effort로 구현하고, 확신 없는 필드명/엔드포인트는
전부 TODO로 표시. **CLAUDE.md 원칙 문구는 사용자 지시대로 건드리지
않았다.**

- **`collectors/dart_client.py` 신규**: `DartClient` — 고유번호(corp_code)
  전체 목록 다운로드+파싱(`corp_code_map()`, zip 안의 XML, 로컬 JSON 캐시
  `.dart_corp_code_cache.json`에 저장해 매번 재다운로드 안 함), 단일회사
  주요계정/전체재무제표(`single_company_accounts()`), 공시검색
  (`disclosure_list()`), 주식총수 현황(`stock_total_count()` — **신뢰도
  가장 낮음**, 발행주식수 필드명 후보 3개를 순서대로 시도하고 전부 실패하면
  None). tr_id·엔드포인트 경로·필드명 전부 모듈 상단에 TODO 주석.
  - **보안**: DART는 KIS와 달리 인증이 헤더가 아니라 **쿼리 파라미터**
    (crtfc_key)라서, 요청 실패 시 `requests` 예외 메시지에 요청 URL이
    그대로 포함되고 거기에 키가 들어 있을 수 있다는 걸 확인하고
    `_mask()`로 모든 예외 메시지에서 키 문자열을 치환하도록 처리함
    (`DartApiError`는 항상 마스킹된 메시지만 담는다). `__repr__`도
    `key_set` 불리언만 노출.
  - **로컬 캐시 + 스로틀**: 호출 간 최소 간격(`_throttle`, 0.1초) +
    "이미 DB에 있는 보고서는 다시 안 받음"(`collect_financials()`의
    `financial_report_exists()` 체크, `force=True`로 강제 재호출 가능).
    정확한 일일 호출 한도는 TODO(공식문서 재확인, 키가 없어 못 봄) —
    대신 재호출을 최소화하는 쪽으로 대응.
  - `rcept_dt`(접수일)가 응답에 직접 없으면 `rcept_no`(접수번호) 앞 8자리
    로 추정한다(비교적 널리 알려진 관행, TODO 표시) — 그마저 안 되면 그
    행은 버린다(미래참조 방지가 핵심이라 날짜를 지어내지 않음).
  - `tests/fixtures/dart_*.json`·`dart_corp_code.zip` 신규(전부 "실제
    응답 아님, 손으로 만든 가짜 데이터" 명시) + `tests/test_dart_client.py`
    40개(1개는 `DART_LIVE_TEST=1`일 때만 — 기본 skip).
- **DB (`db/schema.sql`·`db/store.py`)**: `financials` 테이블 — 유니크 키
  `(corp_code, bsns_year, reprt_code, account_nm)`(사용자 지정 그대로),
  `rcept_dt` NOT NULL(미래참조 방지 핵심). `disclosures` 테이블 —
  `rcept_no` 유니크. `upsert_financials`/`upsert_disclosures`
  (ON CONFLICT 멱등)·`financial_report_exists`·`get_financials`·
  `get_disclosures`·`disclosure_exists`·`get_corp_code_for_stock`
  (이미 수집된 disclosures/financials에서 corp_code 조회, 네트워크 없음)·
  `claim_source_urls`(claims 시딩 멱등성 체크용) 추가. `tests/
  test_dart_store.py` 19개.
- **신호 연결 (`analysis/dart_financials.py` 신규 + `scripts/
  compute_signals.py` 수정)**: `equity_as_of_series`·
  `shares_outstanding_as_of_series`(연결CFS 우선, 없으면 별도OFS)·
  `bps_as_of`·`pbr_series`(전부 `merge_asof(direction="backward")`로
  미래참조 강제)·`quarterly_operating_margin`(매출액·영업이익 **누적
  보고서에서 분기 단독값 역산** — 중간 보고서 누락 시 그 뒤 분기부터는
  역산 체인이 끊겨 계산 안 함)·`quarterly_operating_margin_timeline`.
  **발행주식수는 정식 재무제표 계정이 아니지만, 같은 as-of 조회 패턴을
  재사용하려고 `financials` 테이블에 합성 계정명("발행주식총수")으로
  저장**(`collect_shares_outstanding()`) — 실용적 선택임을 코드 주석에
  명시. `tests/test_dart_financials.py` 18개.
  - **YJH_PBR_FLOOR**: DART(자본총계+발행주식수)로 계산한 PBR이 있으면
    우선 쓰고, 없으면(DART 키 미설정 등) 기존 수동입력 PBR로 **폴백**
    (하위호환 — DART 키 없이도 지금까지처럼 동작, 실제로 돌려봐서 확인함).
  - **YJH_MEMORY_MARGIN**: **중요한 설계 결정** — 기존엔 "마이크론 분기
    매출총이익률"(수동입력)이었는데, 사용자 요청대로 "000660 자체 분기
    영업이익률"(DART 역산)로 **계산 방식 자체가 바뀐다**. DART 데이터가
    있으면 그걸 쓰고, 없으면 기존 마이크론 수동입력으로 폴백한다 — 신호의
    경제적 의미가 소스에 따라 달라진다는 걸 코드 주석에 명시해뒀다(다음에
    "마이크론 지표를 완전히 버릴지" 재검토 필요할 수 있음, 아래 "아직 안
    한 것" 참고).
  - **비교만, 자동 덮어쓰기 없음**(요청 그대로): `_report_dart_vs_manual()`
    이 DART 값과 수동입력 값이 겹치는 날짜가 있으면 차이를 출력만 하고,
    `manual_inputs` 테이블도 신호 계산 결과도 서로를 덮어쓰지 않는다.
    `tests/test_compute_signals_dart.py` 9개(이 프로젝트에서 compute_
    signals.py에 대한 첫 테스트 파일 — 기존 신호 로직은 "CLI는 얇은 배선"
    관행대로 안 건드렸고, 이번에 추가한 DART 분기만 테스트함).
  - 실제 DB로 `python scripts/compute_signals.py` 돌려서 DART 데이터
    없을 때 기존과 **완전히 동일하게**(11개 계산/5개 건너뜀) 동작하는지
    확인함 — 하위호환 검증.
- **공시 → claims 시딩 (`scripts/seed_disclosure_claims.py` 신규)**:
  `disclosures` 테이블(이미 수집된 것만, 네트워크 호출 없음)을 훑어서
  `confidence='official'`(claims에 처음 쓰는 세 번째 값 — DB에 CHECK
  제약이 없어서 코드 변경 없이 바로 씀)로 claims에 기록. summary는 공시
  "보고서명"을 그대로 적음(원문 발언이 아니라 DART가 붙인 문서 제목이라
  원칙 5 "원문 인용 금지" 대상이 아니라고 판단, news.py 헤드라인 저장과
  같은 취급). **원문 링크**는 DART 공개 뷰어 URL
  (`https://dart.fss.or.kr/dsaf001/main.do?rcpNo=...`, 널리 알려진 패턴).
  `price_then`은 "오늘"이 아니라 **공시 접수일 기준 그 시점 가격**(과거
  공시를 지금 시딩하므로 미래참조 방지가 중요 — add_claim.py의 "최신
  종가" 기본 동작과 다르게 구현한 이유를 코드 주석에 명시).
  **멱등성**: claims는 append-only라 DB가 중복을 안 막아줘서,
  `store.claim_source_urls()`로 이미 기록된 공시(URL 기준)를 걸러내고
  스크립트 레이어에서 멱등을 지킨다(재실행해도 중복 안 생김, 테스트로
  확인). `--dry-run` 지원. `tests/test_seed_disclosure_claims.py` 9개.
- **수집 진입점 (`collectors/collect_daily.py`)**: `--only dart` 추가.
  **`dart`는 일부러 기본 실행 목록(`JOB_ORDER`)에 안 넣었다** — 요청대로
  "키 검증 전이니 기본 실행에는 포함하지 마"를 그대로 반영, KIS
  investor_flow를 아직 `collect_daily.py`에 안 묶은 것과 같은 판단
  (4단계 선례). `--only dart`로 명시해야만 실행되고, DART 키가 없으면
  `dart_client.collect_all()`이 네트워크를 전혀 시도하지 않고 0을 반환한다
  — 기본 실행(`python collectors/collect_daily.py`)은 이번 변경과
  무관하게 그대로 동작함을 테스트로 확인.
- `.env.example`에 `DART_API_KEY=` 추가, README.md에 구조 다이어그램·
  사용법·"알려진 제약" 갱신(DART 필드명 신뢰도 낮음을 명시).
- `pytest -q` **471 passed, 2 skipped**(이번 세션 시작 시 374 → 신규 97:
  test_dart_store.py 19·test_dart_client.py 40(39 pass+1 skip)·
  test_dart_financials.py 18·test_compute_signals_dart.py 9·
  test_seed_disclosure_claims.py 9·test_collect_daily.py 신규 3). 2개
  skip은 `KIS_LIVE_TEST=1`·`DART_LIVE_TEST=1`일 때만 도는 실네트워크
  테스트(기본 skip, 공통 규칙 그대로 이번 세션도 실제 DART API를 한 번도
  호출하지 않음).
- **아직 안 한 것**(아래 "DART 키 받은 뒤 확인할 것" 체크리스트와 겹침,
  CLAUDE.md에만 남기고 원칙 문구는 안 건드림):
  - DART_API_KEY 발급 자체(무료, https://opendart.fss.or.kr)와 실제 첫
    호출 검증 — tr_id·필드명 TODO 전부 이걸로만 확정 가능.
  - 발행주식수(`stock_total_count`) 필드명이 전부 추정이라 가장 먼저
    확인 필요 — 틀리면 PBR 계산이 계속 "확인 불가"로 건너뛰어진다(크래시
    는 안 하지만 신호가 안 생김).
  - YJH_MEMORY_MARGIN의 의미가 "마이크론 매출총이익률"에서 "000660
    자체 영업이익률"로 바뀐 것을 사용자가 받아들일지 — 과거 백테스트
    성적(analysis/signal_backtest.py)이 이 신호에 대해 이미 계산해둔
    값은 **이전 정의(마이크론 기준) 때 얻은 것**이라, DART 전환 후에는
    처음부터 다시 쌓여야 의미가 있다(기존 백테스트 성적을 그대로 믿지
    말 것).
  - `collect_daily.py`의 `dart` job을 기본 실행에 언제 포함할지는 실제
    키로 검증한 뒤 사용자가 결정할 일.
  - 재무제표 "정정공시"(기존 보고서 내용이 나중에 바뀌는 경우) 처리는
    이번 범위 밖 — 지금 캐시 로직("이미 있으면 재호출 안 함")은 정정이
    있어도 모르고 넘어간다.

### 완료 (2026-10-13 세션, 1단계 — 급변일 설명 카드)

사용자 요청: 실제 외부 API(KIS·DART·Anthropic) 호출 금지, 단계마다 pytest 통과 확인 후
다음 단계, 원칙 문구는 수정 금지. 이 세션에서 외부 API는 한 번도 호출하지 않았다
(yfinance 수집기도 안 돌림 — DB의 기존 데이터와 합성 데이터만 사용).

- **환경 정리**: `.venv`에 scipy가 없어 6개 테스트 모듈이 수집 단계에서 죽던 상태였다
  (requirements.txt에는 이미 있음). `pip install scipy`로 해결 — 기준선 471 passed, 2 skipped.
- **`analysis/move_attribution.py` 신규(순수 함수)**: 종목 일간 수익률이 ±3%(설정값) 이상인
  날을 찾아 지표별 "평소 베타 × 그날 변화량" = 기대 기여분과 **잔차**를 낸다.
  항등식 `기여분 합 + 잔차 = 실제 수익률`이 테스트로 고정돼 있다.
  - 지표: SOX·엔비디아·미국 10년물·원/달러·VIX·코스피. 미국 지표는 **직전 미국 세션**
    (한국 날짜보다 엄격히 이전), 한국·환율 지표는 같은 날. 미국 10년물은 %p 차이,
    나머지는 %.
  - 베타는 급변일 **이전** 60거래일(급변일 제외)로 추정 → 미래참조 없음. 급변일 이후
    데이터를 바꿔도 결과가 안 변하는지 테스트.
  - 표본수·베타 추정 구간(시작/끝)을 지표마다 반환. 표본 <30이면 기여분에서 빼고 잔차에
    남긴다(`low_samples`). 시세가 없는 지표는 `no_series`, 그날 변화를 못 구하면
    `no_change`, 분산 0이면 `no_beta` — **값을 지어내지 않고 화면에 "데이터 없음"**.
  - **발견·수정한 결함**: `merge_asof`가 지표 수집이 끊긴 뒤에도 몇 달 전 값을 "직전
    미국 세션"으로 끌어왔다(스모크 렌더에서 VIX 변화일이 1년 8개월 전으로 찍힘).
    `MAX_SESSION_GAP_DAYS=5` 허용 간격을 두고 테스트 2개로 고정.
  - 한계를 카드 하단과 모듈 docstring에 그대로 명시: 단변량 베타라 SOX·엔비디아처럼 겹치는
    지표는 합산 시 과대, 코스피는 000660 자체를 포함(순환 — 합성 테스트로 흡수 효과 고정).
- **`db/store.py`**: `get_items_on_dates()` — 급변일의 claim·뉴스·공시(confidence 라벨
  그대로, 공시는 `disclosure`). 없는 날은 키 자체가 없다.
- **대시보드**: "최근 급변일 · 같은 날 함께 움직인 지표" 카드. 문구는 "~때문에"가 아니라
  "같은 날 함께 움직인 정도". 투자자별 수급은 행이 있을 때만(현재 investor_flow 비어 있어
  "데이터 없음"). 현재 실DB에는 SOX·NVDA·VIX 시세가 없어서(미국 수집기 미실행) 해당
  지표는 "시세 데이터 없음"으로 나온다.
- **기존 버그 수정(`web/main.py`)**: `TemplateResponse("index.html", context)`는 설치된
  Starlette 1.7에서 `TypeError: unhashable dict`로 `/`가 500이었다(브라우저로 직접 연 적이
  없어서 안 드러났음). `TemplateResponse(request, "index.html", context)`로 수정.
- 테스트: `tests/test_move_attribution.py` 신규 22개 → 전체 493 passed, 2 skipped.

### 완료 (2026-10-13 세션, 2단계 — weight_history 백필)

- **`analysis/weight_backfill.py` 신규(순수 함수)**: `compute_weights_as_of(close, signals,
  is_on_by_signal, as_of)` — as_of 이하로 **시세와 신호값을 둘 다 자른 뒤** 기존과 같은
  평가·가중치 공식(`evaluate_signal` in-sample → `compute_weights_significant`)을 돌린다.
  `month_end_grid()`는 달력 월말이 아니라 **그 달의 마지막 거래일**을 고른다.
- **`scripts/recompute_weights.py` 리팩터링**: 계산 본체를 위 함수(as_of=None)로 교체 → 실시간과
  백필이 같은 코드를 쓴다. 실DB `--dry-run` 출력을 원본(git HEAD)과 diff해서 **동일**함을 확인.
  tests에는 "실시간 경로 결과 == 공유 함수 결과" 동치 테스트 추가.
- **`weight_history.source` 컬럼**(`'live'`|`'backfill'`, 기존 행은 DEFAULT로 `live`) +
  `(recomputed_on, signal_id)` **부분 유니크 인덱스(source='backfill'만)**. 마이그레이션은 스키마가
  아니라 `_migrate`에서 만든다 — 기존 DB는 ALTER로 컬럼이 먼저 생겨야 해서.
  `insert_weight_history`는 `ON CONFLICT ... WHERE source='backfill' DO NOTHING`: 백필 중복은
  조용히 건너뛰고(기존 행 수정 없음, append-only+멱등), live는 예전 그대로 append,
  NOT NULL 위반 같은 다른 오류는 예외로 올라온다(`INSERT OR IGNORE`는 그것까지 삼키므로 안 씀).
- **`scripts/backfill_weights.py` 신규**: 월 1회(`--every-months`), 시세 3년 이상 쌓인 시점부터
  (`--min-history-years`, 설정값이지 백테스트로 고른 값 아님). 이미 있는 날짜는 계산도 삽입도
  생략하고 old_weight 체인만 잇는다. 백필 구간은 **가장 이른 live 행 날짜 이전**으로 제한
  (`get_current_weights`가 가장 최근 recomputed_on을 쓰므로 현재 가중치를 가로채지 못하게).
  `--dry-run`, `--no-report`. 백필 후 `risk_score_history_report.run()`(신규 분리)을 불러 위험 점수를
  다시 재구성한다. (위험 점수 시계열은 DB에 저장되는 테이블이 아니라 리포트가 매번 재구성하는
  것이라, "다시 생성"은 이 리포트 재실행이다.)
- **실DB 적용 결과**(백업: 세션 scratchpad `baseline.db.before_backfill`; 되돌리려면
  `DELETE FROM weight_history WHERE source='backfill'`): 286개 시점(2003-01-31 ~ 2026-10-02),
  4004행. **냉시작 거래일 6686일(100%) → 803일(12.0%)**. 다시 실행하면 286개 전부 건너뛰고 0행.
  `get_current_weights()`는 그대로(live 행 14개 신호, 6.7%/7.5%).
  - **해석 주의(숨기지 않음)**: 286개 스냅샷 중 **데이터 근거 있음 70개 / 균등 초기값 규칙 216개**.
    적용 거래일로 보면 근거 기반 1448일 vs 균등 초기값 4435일. 즉 냉시작 "기록"은 채워졌지만
    구간의 대부분(약 3/4)은 여전히 냉시작과 같은 숫자(균등 가중치)다. 현재 시점의 실시간 가중치도
    유의한 신호가 하나도 없어(`recompute_weights.py` 출력의 CI하한 ≤ 기준률) 균등 초기값 규칙이다.
  - 냉시작 803일은 시세 시작(2000) + 3년 이전 구간이다.
- **발견·수정**: 저장된 이력이 비어 있을 때 빈 DataFrame과 `concat`하면 `new_weight_pct`가
  object dtype이 되어 `weights_as_of`의 float 컬럼 대입이 `TypeError`로 죽는다(실DB는 live 행이
  있어 우연히 안 드러남). `cold_start_days`에서 숫자 변환 + 빈 프레임 concat 회피.
- **미래참조 테스트**(`tests/test_weight_backfill.py`): t 이후 시세·신호값을 변조해도 t 시점
  가중치·n_events·CI가 그대로, 대조군으로 t 이전을 바꾸면 달라짐. 가정(숨기지 않음):
  signal_values는 각 날짜에 "그 날 알 수 있던 값"이고(signals.py에 `shift(-n)` 없음 확인),
  신호 메타(enabled·weight_cap_pct)는 현재 값을 과거에도 썼다고 본다.
- 테스트: `tests/test_weight_backfill.py` 신규 20개 → 전체 513 passed, 2 skipped.

### 완료 (2026-10-13 세션, 3단계 — 수급 이벤트: 자사주 매입·실적발표·콘퍼런스콜)

- **`events` 테이블**(`db/schema.sql`, 신규 테이블이라 `CREATE IF NOT EXISTS`만으로 기존 DB 승격):
  `(ticker, event_type, start_date, end_date)` 유일. 종류 `buyback`·`earnings`·`conf_call`,
  단일 날짜는 `end_date = start_date`. `source`(`manual`|`dart`)·`source_ref`(접수번호).
  `store.upsert_event`는 멱등이고 갱신 규칙이 비대칭이다 — **manual 입력은 새 값이 있을 때만**
  title·note를 덮어쓰며 source를 manual로 두고, **dart 입력은 빈 칸만 채운다**(사람이 쓴 값을
  자동 추출이 못 덮음). `get_events(ticker, event_type)`.
- **`analysis/events.py`(순수)**: `validate_event`(종류·날짜 정규화, **buyback은 종료일 필수** —
  생략하면 1일짜리로 오인되므로 거부), `normalize_date`(YYYYMMDD·YYYY.MM.DD·'2026년 01월 05일' 등),
  `is_buyback_decision`('자기주식취득결정'·'신탁계약체결결정' 계열, '처분' 제외),
  `parse_buyback_period`, `extract_buyback_events`.
  - **DART 자동 추출의 한계(숨기지 않음)**: 공시검색 목록(`disclosures`)에는 접수일·보고서명만 있고
    **취득 예정 기간이 없다.** 기간을 아는 상세 응답(`details`)이 있을 때만 이벤트를 만들고, 없으면
    `unresolved`(`period_unknown`/`period_unparseable`/`stock_code_missing`)로 사유와 함께 돌려준다 —
    기간을 지어내지 않는다. 정정공시가 같은 기간을 다시 내도 한 번만 만든다.
  - **TODO(키 발급 후 실제 응답으로 확정)**: 보고서명 키워드, 기간 필드명 후보(`aqexpd_bgd/edd`),
    `DartClient.treasury_stock_acquisition_decision`의 경로(`tsstkAqDecsn.json`)·파라미터·응답과
    접수번호 짝짓기. 이 메서드는 **한 번도 호출하지 않았다.** 자기주식취득결과보고서(실제 종료일)는
    상세 필드명을 몰라 범위 밖.
- **`scripts/add_event.py`(수동 입력)**: `--type/--start/--end/--title/--note/--ticker`, `--dry-run`,
  `--list`, `--from-dart`(DB의 disclosures만 읽음, 네트워크 호출 없음 — 테스트에서 `requests.get`을
  폭발시켜 확인). 현재 실DB에는 공시가 0건이라 `--from-dart`는 아무것도 안 만든다.
- **`analysis/event_study.py`(순수)**: 이벤트 종료일 전후 N거래일 수익률·외국인 순매수 합.
  기준일 = 이벤트 날짜 이하 마지막 거래일. 전·후 창을 **따로** 판정(N거래일이 다 안 찬 창은 그 창에서만
  빠짐 → 창마다 n이 다를 수 있고 둘 다 표시). 평균·중앙값·**최대손실(가장 나빴던 이벤트)**·음수 건수,
  n<30이면 `reliable=False`. 외국인 수급은 **창 안 N일이 전부 있을 때만** 합산(일부만 있으면 제외),
  수급 데이터가 없으면 n=0 → 화면 "데이터 없음".
  - **발견·수정한 결함**: 마지막 시세일보다 뒤의 이벤트(아직 안 일어난 것)가 `searchsorted` 때문에
    마지막 거래일에 붙어 "전" 창 표본으로 섞였다. 시세 범위 밖 이벤트는 `n_excluded`로 제외하고 테스트 고정.
- **대시보드 "주시할 이벤트" 카드**: 등록된 이벤트(진행 중 `진행 중 · 종료까지 N일`/`D-N`, 수동/DART 라벨,
  "일정 기록일 뿐 방향 전망 아님") + 자사주 매입 종료 전후 측정 표(5·20거래일, 표본 n·평균·중앙값·최대손실·
  외국인 순매수 평균(n)·표본 부족 배지). 기존 테마 워치리스트는 그대로 아래에 둠. 실DB에는 등록된 이벤트가
  0건이라 현재는 "등록된 이벤트 없음 / 데이터 없음"으로 나온다.
- 테스트: `tests/test_events.py` 31개 + `tests/test_event_study.py` 13개 → 전체 557 passed, 2 skipped.

### 완료 (2026-10-13 세션, 4단계 — 대시보드 렌더링 테스트)

- **`tests/test_dashboard_render.py` 신규(44개)** — FastAPI `TestClient`로 **실제 템플릿까지** 렌더링
  (기존 `tests/test_web.py`는 컨텍스트 구조만 보고 HTML은 안 봤다).
  - **모든 GET 라우트 200**: 라우트를 `app.routes`에서 동적으로 모아서(`/`·`/healthz`·`/docs`·`/redoc`·
    `/openapi.json`·oauth2-redirect) 빈 DB·시세 3행·풍부한 데이터 3가지 상태로 돌린다. `/`·`/healthz`가
    등록돼 있는지 따로 고정. 추가로 **모든 라우트가 GET/HEAD뿐**임을 검사(원칙 2: 쓰기·매매 경로 없음).
  - **주요 카드 존재**(풍부한 데이터): 위험 점수·ADR·반도체 관련주·상관관계·급변일·규칙 백테스트·기준가·
    뉴스·렌즈·에이전트·채점 현황·애널리스트·심리 게이지·캘린더·45일 일정·주시할 이벤트·체크·수집 로그 18개.
    시드한 값(뉴스 제목·claim·에이전트 서술·전문가·신호)이 **실제로 화면에 도달**했는지도 검증해서 누수
    검사가 빈 카드를 검사하는 빈 검사가 되지 않게 했다.
  - **빈 상태**: 빈 DB는 "데이터 없음" 카드 + 수집 명령 안내, 시세 1·2·3·40·120행은 크래시 없이 렌더링되고
    "데이터 없음"·"아직 수집된 뉴스가 없습니다"가 나온다. 주변 지표가 없을 땐 급변일 카드에 "시세 데이터 없음".
  - **nan/None/inf 누수 검사**: style·script·주석을 걷어낸 화면 글자와 태그 속성값에서 `nan|NaN|None|inf|
    -inf|Infinity|null|undefined`(단어 경계, 한글·영문 단어 일부는 제외)를 찾는다. 검사기 자체 테스트(누수 샘플 11개는
    잡고 깨끗한 샘플 8개는 안 잡음) + **음성 대조**(실제 렌더링에 NaN·inf를 주입하면 잡히는지) 포함.
  - 실제 `baseline.db`(읽기만, 네트워크 없음)를 렌더링해도 6개 라우트 전부 200, 누수 0.
- **이 테스트가 잡은 실제 결함 2개(수정함)**:
  1. `analysis/frameworks.py` 팩트체커 렌즈가 사용자 화면에 `값 없음 (NaN 또는 None)`이라는 개발자 문구를
     그대로 내보냈다(시세가 모자란 모든 상태에서 노출) → `데이터 없음 (아직 계산되지 않음)`.
  2. 전문가 `title`이 NULL이면 DataFrame이 NaN으로 읽어 `전문가1 (nan)`으로 찍혔다(`web/main.py`에서
     NaN→None, 템플릿은 제목이 있을 때만 괄호). 같은 표의 적중률 `-%`도 "데이터 없음"으로 바꿈.
  (1단계에서 이미 고친 `TemplateResponse` 시그니처 오류도 이 단계가 드러냈을 종류의 결함이다.)
- **알려진 한계(이번엔 안 고침)**: 000660 시세가 없으면 템플릿 최상위 `{% if not has_data %}`가 "데이터 없음"
  카드 하나만 그리고, 그 밑의 카드(주시할 이벤트·수집 로그·claim 현황 등)는 컨텍스트에 계산돼 있어도 안
  보인다. 렌더링은 깨지지 않으나 시세 없이 등록해 둔 이벤트는 시세가 들어올 때까지 화면에 안 나온다.
- 테스트: 전체 **601 passed, 2 skipped**(세션 시작 기준선 471 → 신규 130: 급변일 22 · 백필 20 · 이벤트 31 ·
  이벤트 스터디 13 · 렌더링 44). 2개 skip은 `KIS_LIVE_TEST`/`DART_LIVE_TEST` 실네트워크 테스트(기본 skip).

### 완료 (2026-10-18 세션 — 최근 2주 위험점수 비중 조절 가상 백테스트)

주문 API 호출 없음. 외부 호출은 yfinance 수집(`collect_daily.py` 기본 job, KIS·DART 제외)뿐.
수집 전 DB 백업: 세션 scratchpad `baseline.db.before_what_if`. `compute_signals.py`로 신호값만 갱신,
가중치 재계산은 안 함.

- **`analysis/what_if.py`(순수)** + **`scripts/what_if_recent.py`(CLI, DB 읽기만)**. t일 종가 상태 →
  t+1일 비중(t일 종가 체결), 가중치는 recomputed_on <= t 행만(같은 날 여러 행이면 id가 큰 쪽 —
  실DB 10/6 live 행이 신호마다 구/신 알고리즘 2개씩 있음). 목표 비중이 바뀔 때만 거래(드리프트 무시),
  비용 = 거래액 × (수수료+슬리피지, 매도면 +세금, 세율은 체결일 기준 `tax_rate_on`). 끝에 청산 안 함.
  단계 매핑: `determine_status()`의 1/3→1단계, 2/3→2단계, KHJ_COMBO→3단계(`--combo-stage`로 변경).
  인자: `--start/--end/--stage-weights/--combo-stage/--initial-weight/--commission/--slippage/
  --context-window/--context-start`.
- **신호 날짜 정렬(새로 다룬 미래참조)**: US10Y 2개·IPO_WEAK·CONCENTRATION·COMBO는 해외 세션
  날짜로 저장되는데 그 종가는 한국 장 마감 뒤에 나온다 → 결정일 t에는 **t보다 엄격히 이전** 값만 씀
  (`PRIOR_SESSION_SIGNALS`), 5일 넘게 묵은 값은 "없음". **기존 `risk_score_history_report.py`
  (`build_is_on_matrix`)는 같은 날짜로 reindex + 없는 날 0으로 채워서 이 반나절 미래참조와 미국 휴장일
  =꺼짐 처리가 남아 있다 — 이번엔 안 고침.**
- **실행 결과(000660)**: 마지막 거래일 2026-10-16. 10/5(개천절 대체공휴일)·10/9 휴장 → 기간은
  10/6~10/16 **8거래일**. 종가 1,841,000→1,272,000. A −23.98%(MDD −24.58%, 비용 0.08%, 변경 2회:
  10/6 66%, 10/15 100%) · B −30.91%(MDD −31.45%) · C 0%. A−B +6.93%p → 2005년 이후 8거래일 구간
  5,375개(겹침, 비겹침 약 671개) 중 99.6 백분위(10거래일 구간 기준 99.4). 과거 구간의 59~62%는
  A−B=0(모델이 내내 100%).
- **해석 시 알아둘 것**: 현재 데이터로는 RATE_STAGE·MEMORY_MARGIN·PBR_FLOOR 값이 없어서(11/14 신호만
  값 있음) KHJ_COMBO 값이 역대 최대 2 → **3단계는 도달 불가**. 10/6 live 가중치로는 켤 수 있는 risk 가중치
  합 최대 56.7% → 점수 최대 78.3 → **2단계(80점)도 도달 불가**. 이번 기간은 정상/1단계만 오갔다.
  10/14에 급락 뒤 opportunity 신호(3DAY_SELLOFF·PANIC_DAY·BOX_BUY)가 켜져 점수가 60 아래로 내려갔고,
  그래서 10/15(−11.06%)는 100% 보유였다. KHJ_COMBO는 CONCENTRATION·IPO_WEAK 중 하나만 켜져도
  is_on=1이라 같은 정보가 점수에 두 번 들어간다(쏠림 그룹에는 미등록).
- 테스트 `tests/test_what_if.py` 22개 → 전체 **623 passed, 2 skipped**.

### 완료 (2026-10-18 세션 2차 — what_if에서 드러난 문제 수정)

외부 API 호출 없음(수집도 안 함, DB 읽기 + weight_history v3 백필 쓰기만). DB 백업: 세션 scratchpad
`baseline.db.before_fix2`. 원칙 문구는 수정하지 않았다.

- **1. 해외 신호 날짜 정렬(공통화)**: `analysis/signal_alignment.py` 신규 — `PRIOR_SESSION_SIGNALS`
  (US10Y 2개·IPO_WEAK·CONCENTRATION·COMBO)는 t일 결정에 t보다 엄격히 이전 값, 원천 휴장일은 직전 값 유지,
  5일 넘게 묵으면 NaN. what_if가 이걸 쓰도록 바꾸고, `risk_score_history_report.build_is_on_matrix`·
  `recompute_weights`·`backfill_weights`·`signal_scorecard`·대시보드 신호별 성적(evaluate_signal 입력)에 적용.
  **대시보드의 "지금 켜진 신호"는 정렬하지 않았다**(보는 시점엔 해외 최신 종가도 이미 아는 정보 — main.py 주석).
  - 신호별 적중률(20일, in-sample 이벤트): YJH_US10Y 80건 52.5% → 39건 41.0%, KSJ_US10Y_HIGH 55건 49.1% →
    29건 41.4%. **예전엔 미국 휴장일마다 신호가 꺼졌다 켜져 이벤트가 쪼개져 있었다.** 유의한 신호는 전후 모두 0개.
  - `tests/test_risk_score_history_report.py`의 `[0,1,0,1,0]`(없는 날=0) 테스트는 새 규칙(`[0,1,1,0,0]`)으로 교체.
- **2. 위험/기회 분리**: `risk_score.compute_risk_score(..., net_opportunity=False)` — 위험 점수 =
  (risk 합+100)/2(하한 50, 임계값 60/80과 그 의미 그대로), 기회 점수 = (opportunity 합+100)/2를 따로 계산.
  상태는 위험 점수만으로. 예전 방식은 `net_opportunity=True`/`what_if_recent.py --legacy-netting`으로 재현.
  대시보드에 위험 점수·기회 지표·상태 3칸, 분해 막대도 둘로 나눔. `reconstruct_score_series`도 같은 기본값,
  `reconstruct_opportunity_series` 추가. `scripts/opportunity_by_risk_state.py` 신규(기회 신호 이후 5/20/63일을
  위험 상태별로, 이벤트 단위, 상태 전체 평균을 기준으로 동반).
- **3. 데이터 정리**: `weight_history.algorithm_version`(1=50% 기준 구 공식, 2=CI 공식+같은 날짜 정렬, 3=2+결정일
  정렬; `store.WEIGHT_ALGORITHM_VERSIONS`). 같은 날·같은 신호면 **큰 버전 → 큰 id**(`get_current_weights`·
  `get_weight_history` 정렬, `risk_score_history.select_weight_rows`). 기존 행은 reason 형식으로 분류(실DB: id 1–14 → 1,
  15–28·백필 → 2), 값은 안 건드림. 백필 유니크 키를 `(recomputed_on, signal_id, algorithm_version)`으로 바꿔
  **v3 백필 4004행을 추가**(v2 행 보존, 재실행 0행 확인). 되돌리기: `DELETE FROM weight_history WHERE source='backfill'
  AND algorithm_version=3`. v3에서 데이터 근거 스냅샷은 286개 중 **8개**(v2는 70개).
  - 원래 리포트의 80~100점 n=2는 10/6 중복 행(구/신 공식)이 정렬 순서 미정이라 구 공식 30% 행이 섞인 결과였다.
  - 대시보드 점수 상한 경고(`risk_score.attainable_ceiling`): 실DB "현재 최대 가능 점수 78점 — 2·3단계 판정 불가
    (누락: KHJ_RATE_STAGE·YJH_MEMORY_MARGIN 수동 입력 없음)".
- **4. what_if 맥락**: 분포를 4개(전체 / A−B≠0 / 데이터 근거 가중치 구간만 / 근거 & ≠0)로, n·비겹침 n·최대 동반.
  `what_if.reason_evidence`·`snapshot_evidence`로 스냅샷별 근거 여부 판별. 이번 기간 자체는 균등 초기값 규칙 구간.
- **숫자(10/6~10/16, 8거래일)**: 예전 방식 A −23.98%(변경 2회) → 분리 A **−20.46%**(변경 1회, 내내 66%), B −30.91%.
  A−B +10.45%p는 4개 분포 모두에서 과거 최대(+9.85%p, 근거 구간은 +4.36%p)보다 커서 백분위 100(근거 구간은 비겹침 19·4개).
- **기회 신호 × 위험 상태(v3, 2005~)**: 1단계일 때 "기회 신호 하나라도" 5일 n=31 평균 +2.16%(상태 전체 +0.96%),
  20일 n=30 +3.48%(상태 전체 +4.38%), 63일 n=26(<30). 개별 신호는 1단계에서 전부 n<30. 2단계 이상은 0일.
- 테스트 신규·수정: test_signal_alignment 7, test_weight_versions 7, test_risk_score +7(분리 3·상한 4, 상계 테스트 3개는
  `net_opportunity=True`로 유지), test_risk_score_history +3, test_what_if +14, test_dashboard_render +2,
  test_risk_score_history_report 1 교체+1 → 전체 **662 passed, 2 skipped**.
- **아직 안 한 것**: live 가중치(10/6, v2)는 다시 계산하지 않았다 — 다음 `run_weekly.py`/`recompute_weights.py`가
  v3 live 행을 쓴다. `reports/signal_scorecard.md`는 재생성하지 않았다(새 정렬로 숫자가 바뀜). 정렬 목록
  (`PRIOR_SESSION_SIGNALS`)은 수동 관리라 해외 원천 신호를 새로 추가하면 여기도 넣어야 한다.

### 2026-10-18 세션 3차 — 실행 계층(executor) 요청: 범위 확인 후 중단

사용자가 `executor/` 자동매매 실행 계층(모드 4개·KisBroker·안전장치·vectorbt 교차검증)을 요청했다. 원칙 2·보안 6번에
해당해 진행 전에 범위를 물었고 **"모의투자 주문까지(실전 URL·tr_id 경로는 만들지 않음, live는 미구현)"**로 확정받았다.
그 직후 사용자가 요청을 중단해서 이 차수에는 코드 변경이 없다. 선행 조건(해외 신호 정렬·위험/기회 분리)은 확인 결과
완료 상태였다(커밋은 안 됨).

### 완료 (2026-10-18 세션 4차 — executor 자본 사다리, 실제 주문 없음)

이 세션에서 외부 API·네트워크 호출 없음, 실제 주문 없음, `.env` 읽지 않음, 원칙 문구 수정 없음. 3차의 범위 결정
(모의투자까지)을 그대로 따른다. 3차 요청이 중단돼서 `executor/`가 없었으므로, 자본 사다리에 필요한 기반
(브로커 인터페이스·FakeBroker·승격 기준·EXECUTOR.md)을 이번에 함께 만들었다. **규칙·안전장치는 `executor/EXECUTOR.md`.**

- **구조**: `executor/`(store·schema·tiers·criteria·metrics·planner·engine·ladder_ops·config_files·web, broker/base·fake,
  scripts/promotion_report·promote_tier·add_contribution·resume), `config/capital_tiers.example.yaml`(실제 값은 gitignore된 `.local.yaml`)·`config/promotion_criteria.yaml`,
  `executor.db`(별도 DB). 루트 `config.py`와 이름이 겹치지만 `config/`에 `__init__.py`가 없어 `import config`는 그대로
  `config.py`(테스트로 고정).
- **요청과 다르게 한 것(의존 방향 규칙 때문)**: CLI를 `scripts/`가 아니라 `executor/scripts/`에 두고(`python -m executor.scripts.…`),
  "자금 배분 현황"을 baseline 대시보드 카드가 아니라 executor 자체 읽기 전용 페이지로 만들었다(`uvicorn executor.web:app --port 8001`).
  baseline 쪽에서 executor를 import하면 의존 방향 규칙이 깨지기 때문.
- **운용 자본 = min(슬리브, 단계 한도)**: "계좌 평가금액"이 아니라 executor 원장으로 계산한 운용분(슬리브)을 쓴다 — 계좌
  평가금액으로 min을 잡으면 손실을 한도 밖 현금이 자동으로 메워서 "손실분을 채우지 않음" 요구와 어긋난다. 한도 밖 현금 =
  계좌 평가금액 − 운용 자본. 매수 수량은 수수료까지 자본 안에 들어오게 계산(안 그러면 매일 1주씩 사고파는 흔들림).
- **승격 수동**(기준 재계산 + 확인 문구 `"<현재>에서 <다음>로 승격합니다"`), **강등 자동**(MDD·괴리·주문 오류·잔고 불일치, 한 단계씩,
  주문 전·후 판정 — 주문 전에 오늘 평가 임시 스냅샷을 남겨 같은 실행에서 자본이 줄게 함). 잔고 불일치는 강등 + 정지(halt),
  해제는 `executor.scripts.resume`. 강등 기준은 미확정이어도 예시값으로 작동, 승격은 미확정이면 불가.
- **배포된 설정으로는 어떤 승격도 불가능**(모든 stage `confirmed: false`, live_2·3 금액 미확정) — 테스트로 고정.
- 테스트: `test_executor_guards.py` 73(의존 방향·이체/출금 식별자 없음+음성 대조·append-only·브로커 5메서드·config 이름) ·
  `test_executor_tiers.py` 14 · `test_executor_scenarios.py` 21(한도 3배 잔고·한도 초과 계획 거부·손실 미보전·MDD 강등과 같은 실행 내
  자본 축소·주문 오류 강등·불일치 정지·자동 재승격 없음·승격 거부 3종·실제 단계 배정·중복 실행·부분 체결·체결 지연·append-only·
  적립 기록·화면 렌더링 누수 0) → 전체 **770 passed, 2 skipped**. `requirements.txt`에 PyYAML 추가.
- **아직 없는 것**: KisBroker(모의투자), 실데이터 섀도 실행기, 파일 킬 스위치·일일 손실/주문 횟수 한도·휴장일 캘린더·데이터 신선도,
  vectorbt 교차검증(EXECUTOR.md 6절). CLAUDE.md 원칙 문구 변경 안은 EXECUTOR.md 7절(미적용).

### 완료 (2026-10-18 세션 5차 — executor 마무리: 섀도 실행기·킬 스위치·KisBroker(모의)·강등 처리 변경)

실제 주문 없음. 네트워크는 차이 분석 요청(6차)에서 허용된 참고 저장소 shallow clone(`../refs/`, 읽기 전용)만 —
한투 공식 샘플을 KisBroker·kis_client 대조에 썼다(코드 복사 없음, 경로·tr_id·필드 이름만). `.env`는 읽지 않았다
(`.env.example` 확인 시도가 권한 거부돼서 KIS_PAPER_* 이름은 EXECUTOR.md에만 적음). 원칙 문구 수정 없음.

- 선행 확인: 해외 신호 정렬·위험/기회 분리 반영 확인(2차 로그·코드·테스트 일치, 미커밋).
- **계좌 분리**: `shadow`·`paper`·`real`(Tier.account). 섀도와 모의투자가 같은 원장을 쓰지 않게. 섀도는 더 높은 단계에서도 비교
  기준으로 계속 돈다. 기존 테스트의 "virtual" 계좌는 "shadow"로 교체.
- **강등 처리 변경(사용자 결정)**: MDD·괴리 → `review_required`(신규 주문 정지·보유 유지·수동 판단), 잔고 불일치 → `halt`
  (정리 포함 모든 주문 정지). 해제는 `executor.scripts.resume`. 주문 오류도 review_required로 묶은 건 Claude 판단(EXECUTOR.md 4절).
  `check_demotion`이 사유 범주(`DemotionReason.category`)를 돌려준다.
- **cap_rejected**: 1회 제외, 연속 N회(config/executor.yaml `cap_rejected_streak`, 기본 3)마다 주문 오류 1건(`engine_runs` 테이블로 셈).
- **비교 종목**: `config/executor.yaml` benchmarks = 000660·069500. MDD 승격 기준은 종목마다 한 항목, 시세 없으면 FAIL.
  `collectors/kr_market.py` TICKERS에 069500.KS 추가(수집은 안 함 — 다음 collect_daily부터).
- **섀도 실행기**(`executor/shadow.py`·`executor.scripts.run_shadow`): 오늘 장 마감 후, baseline 모델 그대로 목표 비중 → 섀도 원장 안
  종가 체결 → `orders_shadow`(멱등)·`shadow_runs`(시도마다 사유). 비교 기준 = 같은 규칙의 what_if 백테스트. 앞으로만(과거 날짜 실행 없음).
- **휴장일 캘린더** `config/krx_holidays.yaml`: 2026년만. 지나간 휴장일은 baseline.db 세 종목 시세 공백과 정확히 일치 확인 —
  **7/17 제헌절이 휴장**이었다는 걸 데이터로 발견(처음 목록에 없었음). 12/25·12/31은 unverified. 범위 밖이면 실행 안 함.
- **킬 스위치**: `KILL_SWITCH` 파일(gitignore 추가) 또는 DB(`executor.scripts.kill_switch`), 실행 시작·주문 하나하나 직전 확인.
- **KisBroker**(`executor/broker/kis.py`, 모의투자 전용 골격): 모의 URL 아니면·모의 키=실전 키면·누락이면 시작 거부. tr_id VTTC0012U/0011U/0013U/
  8434R/0081R(공식 샘플 commit 2077773). 비밀값 마스킹(`executor/redact.py`), 토큰 메모리만. 실호출 테스트는 KIS_PAPER_TEST=1(잔고만).
- **kis_client.py investor_flow TODO 대조**: 경로·tr_id(FHKST01010900)·필드(stck_bsop_date·prsn/frgn/orgn_ntby_qty) **전부 공식 샘플과
  일치**, 단위는 수량(금액은 *_ntby_tr_pbmn). 주석만 갱신(동작 변경 없음).
- **실제 버그 수정(baseline 공통 함수)**: pandas 3에서 `merge_asof`가 날짜 해상도(s·us)가 다르면 MergeError — date 객체로 만든 결정일이
  처음 이 경로를 밟아 드러남. `signal_alignment.align_values`·`what_if.weights_for_dates/snapshot_evidence`·`risk_score_history.weights_as_of`
  에서 ns로 통일 + 회귀 테스트.
- 정적 검사 추가: 주문 엔드포인트 문자열은 `executor/broker/`에만, 실전 주문 tr_id는 저장소 어디에도 없음.
- 테스트: test_executor_shadow 17 · test_executor_kis 20(1 skip) · guards +4 · scenarios +6(강등 3종·cap 연속 2·비교 종목) 등 →
  전체 **818 passed, 3 skipped**.
- 매일 운영 방법(수동 명령·Windows 작업 스케줄러 예시)은 EXECUTOR.md 8절. 스케줄 등록은 하지 않았다.

### 완료 (2026-10-18~19 세션 6차 — 하워드 막스 메모 수집기 · 관점별 토론 섀도 기록)

실제 Anthropic API 호출 없음(dry-run·가짜 클라이언트만), 오크트리 사이트 호출 없음(가짜 HTML fixture만). 원칙 문구 수정 없음.

- **메모 수집기** `collectors/marks_memos.py` + `config/marks_memos.yaml`: 수집 전 (1) 이용약관 — 코드로 판정 불가라 사람이
  `terms_reviewed: true`로 표시하기 전에는 수집 안 함(배포값 false), (2) robots.txt — 매번 코드 확인. 미통과면 collect_log에
  `manual_required`와 사유만. `memos` 테이블(url 유니크, 제목·게시일·본문 해시·수집일·요약) — **본문은 저장 안 함**.
  수동 입력 `--manual`. 페이지 구조는 실제로 확인 못 해서 첫 실행 때 파서 확인 필요(모듈 docstring). `run_weekly.py --only memos`
  (기본 체인에는 미포함).
- **관점별 토론** `agents/debate.py`: 사이클·금리·유동성·가치·추세·반대(실존 인물 이름 없음). 토론 전에 mcp_server/tools 조회로
  스냅샷 JSON 1개를 만들어 sha256 해시를 남기고 모든 관점이 같은 JSON만 본다(위험 점수는 일부러 스냅샷에서 뺌 — 독립 비교용).
  1라운드(4관점) → 2라운드(5관점, 반대 관점 포함 반박) → 종합(JSON 결론 3단계+근거 키). 막스 메모 요약은 사이클 관점 1라운드에만.
  가드레일: `_scan_for_violations()` 재사용 + 결론 값·근거 키·거절·잘림 확인 → 하나라도 걸리면 `debate_shadow`에 안 넣고 `debate_review`.
  모델 기본 `claude-opus-5-5`(effort medium 명시), 거절 대비 서버 측 fallback(`server-side-fallback-2026-07-01`, `fallbacks="default"`)
  켬 — 실제 SDK 객체(`client.beta`)일 때만, 가짜 클라이언트는 `messages.create`.
- `mcp_server/tools.py`에 조회 툴 2개 추가: `get_price_summary`(수익률·이동평균·52주 위치), `get_rates_summary`(미국 10년물 수준·변화 +
  수동 금리 단계, 없으면 "데이터 없음"). server.py에도 등록.
- `agents/memo_summary.py`: 요약 600자 이내, 원문과 40자 이상 같은 구간 있으면 버림, 금지 문구 검사.
- `scripts/run_debate.py`: **기본 dry-run**, `--execute`일 때만 호출. 실행 전 차단 — 같은 (기준일·해시·모델) 이미 있음 / 데이터가
  `max_snapshot_age_days`(5일)보다 오래됨 / 최악 토큰 > `max_tokens_per_run` / 이번 달 비용 + 최악 비용 > `monthly_budget_usd`
  (`config/debate.yaml`). 과거 날짜 옵션 없음. 실DB 사본 dry-run: 호출 10회, 입력 ~8.3만·출력 최대 2.5만 토큰, 최악 $0.83.
- `scripts/debate_scorecard.py`: 결론별 이후 5/20/63거래일 수익률 n·평균·중앙값·최대손실, 같은 날 위험 점수 상태와 일치율(대응 규칙
  config, 교차표 함께), 표본 60 미만 "판단 불가".
- **격리**: executor 코드에 "debate" 문자열·agents import 없음, analysis/·위험 점수 스크립트에 "debate" 없음, 토론 기록을 넣어도 대시보드
  risk_state 동일 — 테스트로 고정. 대시보드 카드 "관점별 토론 (섀도 기록 — 매매 미반영)"(읽기 전용).
- DB: `memos`·`debate_shadow`((decision_date, snapshot_hash, model) 유니크)·`debate_review`(append-only) 신규.
- 테스트: test_marks_memos 11(1 skip) · test_debate 22 · test_run_weekly +3 → 전체 **858 passed, 4 skipped**.
- README에 "메모 요약 개인 참고용·외부 공개 안 함", "과거 날짜 토론 = 미래참조 오염", "자동 스케줄 안 넣음" 명시.

### 완료 (2026-10-18~19 — 참고 오픈소스 차이 분석 + 포지션 정책 통합)

외부 호출은 참고 저장소 shallow clone(`../refs/pysystemtrade` 추가)뿐. KIS·DART·Anthropic 호출 없음, 주문 없음, `.env` 안 읽음.
원칙 문구 수정 없음. **같은 시간에 다른 세션(baseline-07, 6차 로그)도 이 폴더를 고치고 있었다** — 파일은 현재 내용 위에 병합했다.

- **`docs/reference_gap_analysis.md`**: pysystemtrade·NautilusTrader·Lean·한투 open-trading-api와 비교(라이선스·HEAD 날짜 표,
  항목별 위험도, 갖춘 것/빠진 것/과한 것, 가중치 평가, what_if·planner 분리 위험, 선택지 A/B/C, 해외주식·투자자 매매동향 대조).
  권장은 A + vectorbt 교차검증. 우선순위 3가지: ① 비중→수량 함수 통합 ② 체결 시점 결정 ③ 고정 그룹 균등 가중치.
  문서는 실전 주문 tr_id를 적지 않는다(보안 6번).
- **① 구현(이번에 함)**: `analysis/position_policy.py`(순수) — `PositionPolicy(rebalance="on_target_change"|"daily",
  lot_size, buffer, buy_cost_rate)`, `target_quantity()`. what_if.simulate의 드리프트 판정, 신규 `what_if.simulate_shares`
  (정수 주식, 엔진과 같은 원장 계산), `executor/planner.plan_orders`(last_targets·policy 인자), `engine.run_once`(policy 인자,
  `target_history`에서 직전 목표를 읽고, 전부 체결됐을 때만 기록), `shadow.backtest_reference`(simulate_shares + `shadow_policy()`)가
  전부 이 함수를 쓴다.
  - 기본 정책 = 기존 what_if 규칙(목표 변경 시에만, 드리프트 무시). 예전 planner는 매일 드리프트를 맞췄다(66% 고정, 가격 +5% →
    매도 2주) — 이제는 `rebalance="daily"`로만 재현된다. 보유 평가액이 운용 자본을 넘으면(이익이 한도를 넘거나, 손실·강등으로 자본이
    줄었을 때) 같은 목표여도 다시 계산한다(한도 검사가 보유만으로 거부하지 않게).
  - 섀도 비교 기준은 이제 첫날도 같은 종가에 산다(예전엔 하루 늦게 시작해서 첫날 기준 = 배정액). 섀도 테스트는 "0.5%p 미만"에서
    **매일 운용분 = 기준(정확히)**으로 바뀌었다.
  - what_if_recent(10/6~10/16) 재실행 결과 동일: A −20.46%, A−B +10.45%p.
  - 버퍼(`buffer`)는 기본 0(끔) — 켤지와 크기는 사용자 결정.
- 테스트: `tests/test_position_policy.py` 신규 22개(정책·planner 드리프트 미거래·target_history 기록 조건·백테스트↔엔진 수량·자산 정확
  일치), test_executor_shadow 2개 수정 → 전체 **881 passed, 4 skipped**.
- **아직 안 한 것(사용자 결정 필요)**: ② 체결 시점(장후 시간외 `06` vs t+1 시가 — 결정 전에는 KisBroker로 모의 주문 보내지 않기),
  ③ 가중치 그룹·임계값(문서 6.2의 그룹 표는 예시), 버퍼 사용 여부.

### 완료 (2026-10-19 세션 baseline-07 — 차이 분석 (2): freqtrade·Lean·Nautilus·한투, 문서만)

다른 세션(baseline-3c)이 같은 작업 트리에서 동시에 작업 중이었다. 그쪽이 이미 쓴 `docs/reference_gap_analysis.md`(문서 1,
pysystemtrade 기준)는 건드리지 않고, 사용자 선택에 따라 **별도 파일 `docs/reference_gap_analysis_freqtrade.md`**(문서 2)로 썼다.
코드 수정 없음. 참고 저장소는 `../refs/`에 shallow clone(읽기 전용, 코드 복사 없음).

- 라이선스: 한투 LICENSE 없음(복사 불가) · freqtrade GPL-3.0 · Lean Apache-2.0 · Nautilus LGPL-3.0.
- 핵심 발견: (1) **재시작 복구 없음**(Broker에 미체결 조회 없음, planned/submitted 재확인 없음 — 셋 다 시작 시 복구를 기본으로 가짐, 높음),
  (2) 체결 시점 가정(t 종가)과 실제 주문(장 마감 뒤 시장가 `01`)이 맞지 않음(`06` 장후 시간외·`07` 시간외 단일가 존재, 높음 — 문서 1 N-9와 같음),
  (3) 신호→단계→목표 비중 경로가 네 곳에 복사(what_if_recent·shadow·debate_scorecard·opportunity_by_risk_state, 중간 — 비중→수량은 position_policy로 이미 통합),
  (4) 일일 손실·일일 주문 횟수·쿨다운·REDUCING 상태 없음(중간), (5) 단일 종목 100%는 참고 프로젝트들이 기본으로 막는 형태(설계 결정).
- 로드맵 R-1~R-9(전부 "우리 코드 수정" 권장, 프레임워크 교체는 지금 아님 — Nautilus·Lean 모두 KIS 어댑터를 직접 만들어야 하고 Lean은
  KRX 주식 장 운영시간 데이터도 없음, freqtrade는 ccxt 전용이라 불가). 구현은 사용자가 고른 뒤.

### 완료 (2026-10-19 세션 baseline-07 2차 — 커밋 정리 · 체결 시점 next_open · 주문 단위 · 재시작 복구 · 운영 제한)

실제 주문·네트워크 호출 없음, `.env` 안 읽음, 원칙 문구 수정 없음, **push 안 함**(커밋만). baseline-3c 세션은 이 작업 동안
멈춰 있었다. 단계마다 `pytest -q` 확인 후 커밋.

- **0. 커밋 정리**: 그동안 미커밋이던 baseline-07·baseline-3c 작업을 의미 단위 4개 커밋으로 나눔(what_if·정렬·분리·버전·포지션 정책 /
  executor / 막스 메모·토론 / 문서). `.env`·`executor.db`·토큰 캐시 제외 확인. pre-commit 훅이 예시 비밀값 형식에 걸려서 가짜 값을
  10자 미만·자리표시로 바꿨다(훅 우회 안 함). `.gitignore`의 `data/manual/*.csv` 줄 끝 주석 때문에 규칙이 안 먹던 것도 고침.
- **1. 숫자 차이 설명**(10/6~10/16): 예전 상계 방식 A −23.98%/A−B +6.93%p(변경 2회) vs 분리 방식 −20.46%/+10.45%p(변경 1회).
  상계 + v2 가중치 + 결정일 정렬 → v3로 바꿔도 동일 → 정렬을 꺼도 동일 → **분리만 바꾸면 −20.46%**. 즉 차이 전부가 위험/기회 분리에서
  나온다. baseline-3c 로그의 "재실행 결과 동일"은 position_policy 전/후 비교(둘 다 분리 이후)라 맞지만 문맥 없이 읽으면 헷갈린다.
- **2. 체결 시점 기본값 = t일 종가 결정 → t+1일 시가 체결**(당일 종가는 `fill: same_close` 옵션). 엔진 두 단계(`run_once`는 계획만
  `planned` + `pending_decisions`, `execute_pending`이 다음 시가에 체결 — 오래된 결정은 expired, 시가 갭 상승으로 현금이 모자라면
  살 수 있는 만큼만 `qty_reduced`, engine_runs는 안 씀). what_if `simulate_next_open`·`simulate_shares(fill=)`가 같은 규칙, 섀도 =
  비교 기준 정확히 일치 유지. 섀도는 매일 결정 종가 vs 다음 날 시가 갭을 `shadow_gaps`에 기록. 10/6~10/16: next_open A −20.57%
  (MDD −20.93%, A−B +10.34%p) vs same_close −20.46%(+10.45%p), 갭 −8.10%(10/15)~+2.22%.
- **3. 주문 단위 점검**: `scripts/lot_size_table.py`, `planner.weight_gap_warnings`(목표 vs 실현 비중 차이 > `max_weight_gap_pctp`
  기본 10%p면 `weight_gap_warning` 사건, 주문은 막지 않음). 운용 자본 200만원(예시 계산)·000660 1,272,000원: 100%→1주(63.6%, ⚠+36.4%p),
  66%→1주(63.6%), 33%→0주(⚠+33%p), 0%→0주. 069500은 DB에 시세가 없어 **가정 가격**(2/4/6만원)으로만 계산 — 오차 1~3%p.
  SPY·QQQ(가정 $600/$500, 0.0001주 단위)는 0%p. **이 커밋은 테스트 1개가 깨진 채 들어갔다가 `--amend`로 고쳤다**(pytest 출력을
  tail로만 봐서 놓침 — 이후로는 pytest를 따로 돌려 확인 후 커밋). push 전이라 이력 외부 영향 없음.
- **4. 신호 그룹 균등 가중치 설계안**(`docs/signal_group_weighting_design.md`, **구현 안 함**): 실측 상관(COMBO~CONCENTRATION 0.91,
  US10Y 둘 0.55, BREADTH~BOX_TRIM 0.42) 근거로 위험 그룹 R1 금리·R2 글로벌 위험선호·R3 000660 과열·R4 반도체 쏠림·R5 메모리 업황,
  기회 O1~O3. 점수 = 켜진 위험 그룹 수 / 값 있는 그룹 수. 단계 기준은 기준 구간(2005~2019) 분포의 백분위로 정해 검증 구간(2020~)에서
  다시 맞추지 않고 보고(대안 walk-forward). 그룹 수가 많다고 이후 20일 수익률이 나빠지지 않았다는 사실도 같이 기록. **사용자 확정 대기.**
- **5. 포지션 버퍼**: 기본 0 그대로(변경 없음).
- **6-1. 문서 통합**: 차이 분석 2건 → `docs/reference_gap_analysis.md`(통합본, 출처 [1]/[2], 결론이 다른 곳 ⚑, 권고마다 상태 열),
  원본은 `docs/archive/`. 절 번호는 문서 1을 따름(코드가 7절·N-9를 가리킴). EXECUTOR.md의 낡은 체결 시점 문구도 갱신.
- **6-2. 재시작 복구**(`executor/recovery.py`): Broker에 조회 전용 `list_orders` 추가(KIS VTTC0081R, 페이지 넘김·`rmn_qty`는 TODO).
  주문 상태를 `sending`(전송 직전) → `submitted`(접수) → 확정 뒤 끝난 상태로 기록. 시작 시 `sending`/`submitted`를 브로커와 맞춰
  끝난 주문(체결·취소 확인·거부)만 자동 확정(원장에 없는 체결분만 기록), 살아 있는 잔량·짝 0/2+개·수량 역전·조회 실패는 halt +
  `recovery_manual`. 복구는 주문·취소를 하지 않는다. 테스트: 전송 직후 종료 → 재시작, 부분 체결 중 종료 → 재시작(잔량 정지 → 사람이
  취소 → 확정). 가드 테스트의 "브로커 메서드 5개"는 6개로 갱신(자금 이동 메서드 없음은 그대로).
- **6-3. 일일 한도**(`config/executor.yaml daily_limits`, 미확정): 손실률 3%(그날 매수 보류) · 주문 4건(초과 시 계획 전체 거부,
  상태 `daily_limit`) · 쿨다운 5일(강등·정지 사건일 기준, 매수 보류). 매도는 진행. 판정은 executor.db에서 계산해 재시작해도 유지.
- **6-4. 종목당 최대 비중**(`concentration`, 미확정): 30%, 예외 069500·SPY·QQQ. 엔진은 목표를 맨 처음 자르고, 섀도는 단계 비중에
  먼저 적용(비교 기준과 일치 유지). **현재 설정으로 000660 단계 100/66/33%가 모두 30%가 되고, 운용 자본이 약 423만원 미만이면 0주**(1주가 30%보다
  큼) — 이 기본값이면 섀도가 000660을 사지 않는다. 사용자가 상한·한도·종목을 같이 정해야 의미가 생긴다.
- **6-5. 휴장일 알림**: 12/1 이후 다음 해 목록이 없으면 run_shadow가 `calendar_warning` 사건·CLI 출력, executor 화면 맨 위 경고.
- 테스트: 901 → **933 passed, 4 skipped**(fill 관련 재작성, recovery 10, limits 15, KIS list_orders 1, 휴장일 6, 섀도 상한 1 등).

### 완료 (2026-10-19 세션 baseline-07 3차 — 숫자 분해 · 069500 섀도 · 그룹 방식 기본값 · 국장/미장 비교)

실제 주문·네트워크 호출 없음(yfinance 시세 수집 069500·SPY·QQQ만). **push 안 함.** 이번부터 커밋 전 pytest 전체 요약 줄에서
failed 0을 확인한 뒤에만 커밋. 원칙 문구 수정 없음.

- **1. what_if 숫자 분해(10/6~10/16, 000660)**: 3a0a7b0 한 커밋에 여러 변경이 묶여 있어 커밋을 되돌리는 대신 현재 코드의
  스위치로 계산(세션 scratchpad `decompose_whatif2.py`). ⓪ 원래 조건(상계·가중치 v≤2·해외 신호 t 이전·당일 종가) A −23.98% / A−B +6.93%p
  (원래 출력 `whatif_before.txt`와 일치) → ① 해외 신호 정렬을 꺼도 동일(이 기간엔 영향 0) → ② 위험/기회 분리만 켜면 −20.46% / +10.45%p
  (차이 전부) → ③ v3 가중치 행 포함 동일 → position_policy 전후 동일(분리 후 출력 `whatif_sep`·`whatif_final_sep` 같음, 비율 백테스트는
  원래 같은 규칙) → ④ 다음 날 시가 체결 −20.57% / +10.34%p(MDD −20.93%). 아래 4번 이후 기본(그룹 방식)으로는 −9.78% / +21.13%p.
- **2. 069500**: yfinance는 2007-01-29부터만 준다(2002 상장일 아님). 배당일 등의 가격 없는 행 5개가 섞여 들어와 수집기 3개가 NaN 행을
  버리게 고치고 삭제. 섀도 대상 `shadow.ticker = 069500`(지수 ETF라 종목당 상한 예외, 단계 비중 그대로), 000660은 비교 기준.
  lot_size_table 실제 가격(89,930원)으로 오차 1.1~3.0%p. 신호 적용성·제외/대체 제안은 `docs/signal_applicability_069500.md`(구현 안 함).
- **4. 그룹 방식 기본값**(`analysis/group_score.py`): 설계안 6.2절 그룹, 그룹 안·그룹끼리 균등, 기준 기간 2005~2018(한국 거래일) 상위
  30/15/5% → 기준점 29.2/41.7/54.2점(동점 때문에 실제 24.4/7.8/3.2%). what_if_recent `--method`, 섀도 `score_method`, 대시보드 "오늘의
  상태", debate_scorecard가 그룹 방식, 적중률 가중치는 옵션(대시보드에 참고로 표시). KHJ_COMBO==3 덮어쓰기는 그룹 방식에 없음.
  비교(`scripts/group_score_report.py`): 단계 비중 운용은 기준 기간 두 방식 모두 단순 보유보다 낮았고(+273.8%/+305.6% vs +419.3%),
  검증 기간은 그룹 +1826.8% · 가중치 +2024.5% vs 보유 +2002.5%(MDD는 −42.0%/−44.7% vs −56.8%). 기준값은 바꾸지 않았다.
  **원칙 1 본문의 "60/80점, DEFAULT_RISK_THRESHOLD" 서술이 이제 기본 방식과 다르다 — 원칙 문구 변경안은 사용자 확정 필요(적용 안 함).**
- **3. 국장/미장 비교**(`scripts/market_compare.py`, `reports/market_compare.md`): 실제 USD/KRW로 원화 환산, 가격 수익률(배당 미포함),
  해외 양도세는 `config/market_compare.yaml`(최신 세법 확인 필요). C(위험점수)는 네 대상 모두 전체 기간 연환산이 A보다 낮았고, 해외는
  기준점에 동점이 생겨 1단계가 사라진다(50.0/50.0/58.3). 금액은 계산용 가정값.
- 테스트 936 → **965 passed, 4 skipped**.

### 미완 (다음 할 일)
- [ ] KIS API 키 발급 (**모의투자 계좌로**) — 이제 **수급 데이터 전용**으로만 필요
- [ ] `kis_client.daily_prices()` — 사실상 불필요해짐 (yfinance가 000660.KS 커버).
      필요하면 KIS쪽이 더 정확한 실시간 호가일 때만 교체 검토
- [x] `kis_client.investor_flow()` — 2026-10-06 4차 세션에서 키 없이 구현 완료(파싱
      로직은 fixture로 검증됨). **실제 키로 호출해 TODO의 tr_id·필드명이 맞는지
      재확인하는 것은 여전히 남아 있음** — 키 발급 후 가장 먼저 할 일
- [ ] Claude Desktop/Code에 MCP 서버를 실제로 연결해서 툴 호출까지 end-to-end로
      확인(임포트·기동은 확인됐지만 실제 클라이언트 연동은 아직)
- [ ] 대시보드는 이번 세션에 실제 fastapi/jinja2 설치 후 `build_dashboard_context()`
      + 진짜 템플릿 렌더링으로 확인 완료(합성 데이터). 다만 `uvicorn web.main:app
      --reload`로 브라우저에서 직접 열어본 적은 없음 — 특히 yfinance가 실제
      네트워크로 000660.KS/SKHY/KRW=X를 반환하는지는 여전히 미확인.
- [ ] ADR 전환비율(1:10) 공식 예탁계약서로 재확인
- [ ] GitHub Actions 배치 (이제 kr_market.py·news.py도 스케줄에 포함)
- [ ] `scripts/add_claim.py`로 실제 뉴스/소문 몇 건 기록하고
      `scripts/grade_claims.py`로 만기 도래 후 채점까지 화면에서 확인
- [ ] **`ANTHROPIC_API_KEY` 발급받아서 `python scripts/run_agents.py --ticker 000660`
      실제로 한 번 돌려보기** — 이 세션은 진짜 API를 한 번도 안 불러서(비용 0 유지),
      실제 Claude 응답이 시스템 프롬프트를 잘 지키는지·`_scan_for_violations()`가
      오탐/미탐 없이 잘 작동하는지는 아직 실제 호출로 확인 못 함. 먼저 `--dry-run`으로
      전송 내용부터 확인할 것
- [ ] 에이전트 배치도 GitHub Actions 스케줄에 넣을지 결정 (넣으면 자동으로 매일
      API 비용이 나가므로, 우선 수동 실행으로 남겨두는 게 안전할 수 있음)
- [ ] 지금은 종목 하나(000660)만 분석하게 돼 있음 — 반도체 관련주(NVDA 등)도
      같은 방식으로 확장할지는 다음에 필요하면
- [ ] 규칙 백테스트(`scripts/backtest_strategies.py`)는 과거 측정이라 원칙 1과
      충돌하지 않지만, **실매매 연동·알림("오늘의 신호") 단계는 아직
      미구현**이고 원칙 2 때문에 의도적으로 손대지 않았다. 진행하게 되면
      **모의투자 계좌로 먼저** 검증할 것 — 실제 계좌 연동은 그 다음.
- [x] 규칙 백테스트를 대시보드/MCP 서버에 연결 — 2026-10-05 2차 세션에서 완료
      (`get_rule_backtest` 툴, 대시보드 카드). 둘 다 n<30 경고를 숨기지 않음.

---

## 개발 로드맵

| 주차 | 내용 | 예상 |
|---|---|---|
| 1 | 스캐폴딩·분석 모듈·미장 수집 | ✅ 완료 |
| 2 | KIS 연동, 하이닉스 시세·수급 | 6~8h |
| 3 | FastAPI 대시보드 | 8~10h |
| 4 | GitHub Actions + 클라우드 DB | 5~6h |
| 5 | **MCP 서버** | 8~10h |
| 6 | 백테스트·전문가 채점 | 6~8h |
| 7~8 | 멀티 에이전트 (선택) | 2~3일 |

총 40~50시간 예상.

---

## 기술 스택 / 환경

- Python 3.11+, pandas, requests, yfinance
- SQLite (4주차에 Supabase/Neon 검토)
- 개발: 맥북 / 배치: 아이맥 또는 GitHub Actions
  - **맥은 뚜껑 닫으면 launchd가 안 돈다** → Actions 권장
- 감시 종목: SK하이닉스(000660), SOX, NVDA, MU, TSM, AMD

### 비밀값 취급
- `.env`는 **절대 커밋 금지** (`.gitignore`에 포함됨)
- `.kis_token.json`도 커밋 금지
- `*.db` 커밋 금지 (바이너리 충돌)

---

## 멀티 에이전트 설계 (7주차 예정)

실존 인물을 복제하지 **않는다**. 인물명 대신 투자 철학 이름을 쓴다.
(저작권·인격권 문제 회피 + 원칙 자체에 집중)

| 관점 | 초점 |
|---|---|
| 가치 | 해자·안전마진 |
| 성장 | PEG·성장 지속성 |
| **사이클** | 지금 사이클의 어디인가 (하이닉스에 가장 중요) |
| **밸류에이션** | 현재 주가에 내재된 가정 역산 |
| 리스크 | 최대 손실 시나리오 |
| 팩트체커 | 근거가 DB 데이터와 일치하는지 검증 |

**출력 형식**: "살까 말까"가 아니라 **"이 원칙에서 무엇이 걸리는가"**.
충족 / 미충족 / 확인 필요 세 가지로 분류한다.

**주의**: 에이전트 4명이 같은 결론을 내도 그것은 독립된 판단 4개가 아니라
같은 모델이 4번 답한 것이다. 만장일치를 확신의 근거로 쓰지 않는다.
에이전트 의견도 `claims` 테이블에 저장해 나중에 채점한다.

---

## 작업 시 참고

- 새 기능 추가 전 `pytest -q`로 회귀 확인
- `analysis/` 모듈은 **순수 함수 유지**. DB·API를 import하지 않는다
- 커밋은 기능 단위로. 잔디 채우려고 의미 없는 커밋 만들지 않는다
- 저장소는 private으로 시작. public 전환 전 키 흔적 정리
