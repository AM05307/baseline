-- baseline schema (SQLite)
-- 수집한 원본만 저장한다. 계산 결과는 저장하지 않는다.

CREATE TABLE IF NOT EXISTS daily_price (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,          -- YYYY-MM-DD
    open    REAL, high REAL, low REAL, close REAL,
    volume  INTEGER,
    source  TEXT,                   -- 'kis' | 'yfinance'
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS investor_flow (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,
    individual  INTEGER,
    foreigner   INTEGER,
    institution INTEGER,
    PRIMARY KEY (ticker, date)
);

-- 전문가 발언 아카이브. '신호'가 아니라 '기록'이다.
-- 나중에 적중 여부를 채점하기 위해 예측 시점의 주가를 함께 남긴다.
CREATE TABLE IF NOT EXISTS claims (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    said_on      TEXT NOT NULL,
    who          TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    summary      TEXT NOT NULL,     -- 원문 인용 금지. 본인 요약 한 줄만.
    source_url   TEXT,
    price_then   REAL,
    target_price REAL,
    horizon_days INTEGER,
    graded_on    TEXT,
    price_after  REAL,
    hit          INTEGER,           -- 1 hit / 0 miss / NULL 미채점
    confidence   TEXT NOT NULL DEFAULT 'news',  -- 'news'(출처 확인) | 'rumor'(미확인)
    affiliation  TEXT,              -- 소속(예: '어느 증권사', '어느 유튜브 채널')
    stance       TEXT               -- '매수' | '중립' | '매도'. 1인칭 발언 재구성 아니라 분류 라벨.
);

-- 뉴스/소문 헤드라인. summary는 여기서도 원문 그대로 긁어오지 않고
-- 제목만 저장한다 (짧은 제목은 저작권 리스크가 낮다고 보되, 본문 전재는 하지 않는다).
-- confidence='rumor'인 항목은 화면에서 반드시 "미확인" 라벨을 붙인다.
CREATE TABLE IF NOT EXISTS news_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    headline     TEXT NOT NULL,
    source       TEXT,
    url          TEXT,
    published_at TEXT NOT NULL,     -- ISO date/datetime
    confidence   TEXT NOT NULL DEFAULT 'news',  -- 'news' | 'rumor'
    collected_at TEXT NOT NULL,
    UNIQUE(ticker, url)
);

CREATE TABLE IF NOT EXISTS collect_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at    TEXT NOT NULL,
    job       TEXT NOT NULL,
    status    TEXT NOT NULL,        -- ok | fail
    rows      INTEGER DEFAULT 0,
    message   TEXT
);

-- 에이전트(LLM) 배치 분석 결과 캐시.
-- web/mcp_server는 이 테이블만 '읽는다' — 대시보드를 열거나 MCP 툴을 호출하는
-- 것만으로는 절대 API 비용이 발생하지 않는다. 비용은 scripts/run_agents.py를
-- 사람이 직접 실행할 때만 생긴다. narrative는 에이전트가 쓴 한국어 서술이고,
-- tool_calls는 그 서술의 근거로 실제 호출한 툴과 반환값을 JSON으로 남긴 감사
-- 기록이다 (나중에 "무슨 데이터를 보고 이렇게 썼는지" 되짚어볼 수 있게).
CREATE TABLE IF NOT EXISTS agent_analyses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    run_date    TEXT NOT NULL,        -- YYYY-MM-DD. 같은 날 재실행하면 갱신됨(원칙 6)
    model       TEXT NOT NULL,
    narrative   TEXT NOT NULL,
    tool_calls  TEXT NOT NULL,        -- JSON 배열: [{name, arguments, result}, ...]
    warnings    TEXT,                 -- JSON 배열. 금지 문구 검출 등 자체 점검 결과
    created_at  TEXT NOT NULL,
    UNIQUE(ticker, run_date)
);

-- 전문가 경험칙 신호 시스템. '신호'는 전문가 경험칙을 analysis/signals.py의
-- 순수 함수가 과거/현재 데이터로 기계적으로 계산한 on/off(+수치)다 — LLM이
-- 만든 서술이 아니다. CLAUDE.md 원칙 1(갱신판)과 같은 선상: 백테스트로
-- 성적을 추적하고(analysis/signal_backtest.py), 그 성적만큼만 가중치를 준다.
CREATE TABLE IF NOT EXISTS experts (
    expert_id   TEXT PRIMARY KEY,   -- 'KHJ' 같은 짧은 코드
    name        TEXT NOT NULL,
    title       TEXT,               -- 직함/소속 요약
    source_show TEXT,               -- 출처 방송/채널명
    note        TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id      TEXT PRIMARY KEY,  -- 'KHJ_3DAY_SELLOFF' 같은 코드
    name           TEXT NOT NULL,
    expert_id      TEXT NOT NULL REFERENCES experts(expert_id),
    source_ref     TEXT,              -- 영상/회차
    kind           TEXT NOT NULL,     -- 'regime_filter' | 'trade_rule' | 'event'
    direction      TEXT,              -- 'risk' | 'opportunity' | NULL(event은 방향 없음)
    data_source    TEXT,              -- 참고용 설명 (예: '000660, KOSDAQ 등')
    is_manual      INTEGER NOT NULL DEFAULT 0,  -- 1이면 manual_inputs에 의존
    description    TEXT NOT NULL,     -- 규칙 설명(본인 요약, 원문 인용 금지)
    weight_cap_pct REAL,              -- 이 신호 전용 가중치 상한(%). NULL이면 기본 상한
    enabled        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS signal_values (
    date      TEXT NOT NULL,
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
    value     REAL,             -- 수치(예: 마이너스 시장 수). NULL 허용(계산 불가한 날)
    is_on     INTEGER NOT NULL, -- 0/1
    PRIMARY KEY (date, signal_id)
);

-- source='backfill' 행은 scripts/backfill_weights.py가 "그 시점까지의 데이터만으로"
-- 재구성한 것이다(recomputed_on = 그 시점의 마지막 거래일). (recomputed_on, signal_id)
-- 유일성 인덱스는 백필 행에만 걸어(db/store.py _migrate — 기존 DB에는 source 컬럼이
-- ALTER로 먼저 생겨야 해서 스키마가 아니라 마이그레이션에서 만든다) 재실행해도
-- 중복되지 않는다. live 행에는 유일성 제약이 없다(기존 동작 그대로).
-- 가중치 변경 이력. 매주 재계산마다 신호별로 이전값→새값을 남긴다(원칙 6과
-- 별개로, 가중치 자체는 "현재 상태"가 아니라 "변경 기록"이 목적이라 append-only).
CREATE TABLE IF NOT EXISTS weight_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    recomputed_on  TEXT NOT NULL,
    signal_id      TEXT NOT NULL,
    old_weight_pct REAL,
    new_weight_pct REAL NOT NULL,
    reason         TEXT,
    source         TEXT NOT NULL DEFAULT 'live',  -- 'live'(실시간 재계산) | 'backfill'(과거 시점 재구성)
    algorithm_version INTEGER  -- 가중치 산정 방식 버전(db/store.py WEIGHT_ALGORITHM_VERSIONS). 같은 날·같은 신호면 큰 버전 우선
);

-- 수동 입력값의 타임라인(금리 단계·마이크론 매출총이익률·000660 선행 PBR·
-- 이벤트 날짜). 덮어쓰지 않고 계속 쌓는다("입력 이력 남기기") — 같은
-- input_type의 "현재 유효한 값"은 조회 시점에 effective_on이 가장 최근인
-- 행을 고른다(db/store.py의 as-of 조회 함수가 담당, 여기는 저장만).
CREATE TABLE IF NOT EXISTS manual_inputs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    input_type   TEXT NOT NULL,   -- 'rate_stage' | 'memory_margin' | 'pbr' | 'event'
    effective_on TEXT NOT NULL,   -- 이 값이 적용되는 기준일
    value_text   TEXT,            -- 범주형 값(예: rate_stage 단계명, event 설명)
    value_num    REAL,            -- 수치형 값(예: margin %, PBR)
    note         TEXT,
    entered_at   TEXT NOT NULL    -- 실제로 이 레코드를 입력한 시각
);

-- DART(전자공시) 연동. collectors/dart_client.py가 채운다 — 조회 전용 API라
-- 매매와는 애초에 무관(CLAUDE.md 원칙 2). 원본 재무계정만 저장하고, PBR/영업
-- 이익률 같은 파생값은 analysis/dart_financials.py가 그때그때 계산한다
-- (schema 주석 1행: "수집한 원본만 저장한다"와 같은 원칙).
--
-- financials의 유니크 키는 (corp_code, bsns_year, reprt_code, account_nm)
-- 4개만 쓴다(사용자 지정) — 같은 계정명이 연결(CFS)·별도(OFS) 양쪽에 다른
-- 값으로 존재하는 드문 경우 나중 upsert가 덮어쓴다(fs_div는 참고 컬럼으로만
-- 같이 저장, 유니크 키에는 안 넣음 — analysis/dart_financials.py가 선택
-- 로직을 문서화함).
CREATE TABLE IF NOT EXISTS financials (
    corp_code     TEXT NOT NULL,
    bsns_year     TEXT NOT NULL,   -- 사업연도 'YYYY'
    reprt_code    TEXT NOT NULL,   -- 보고서코드(11013=1분기·11012=반기·11014=3분기·11011=사업보고서)
    account_nm    TEXT NOT NULL,   -- 계정명(예: '자본총계'·'매출액'·'영업이익')
    fs_div        TEXT,            -- 'CFS'(연결)|'OFS'(별도) — 참고용, TODO 공식문서 재확인
    sj_div        TEXT,            -- 재무제표구분(BS/IS 등) — 참고용, TODO 공식문서 재확인
    thstrm_amount REAL,            -- 당기 금액(원본 그대로, 단위 TODO 공식문서 재확인)
    stock_code    TEXT,
    rcept_no      TEXT,            -- 이 금액이 포함된 공시의 접수번호
    rcept_dt      TEXT NOT NULL,   -- 공시 접수일(YYYY-MM-DD) — 미래참조 방지의 핵심(그 날짜 전엔 이 값을 몰랐다)
    source        TEXT DEFAULT 'dart',
    PRIMARY KEY (corp_code, bsns_year, reprt_code, account_nm)
);

CREATE TABLE IF NOT EXISTS disclosures (
    rcept_no   TEXT PRIMARY KEY,   -- 공시 접수번호(DART 고유 식별자)
    corp_code  TEXT,
    corp_name  TEXT,
    stock_code TEXT,
    report_nm  TEXT,               -- 보고서명(예: '분기보고서 (2026.09)')
    rcept_dt   TEXT NOT NULL,      -- 접수일(YYYY-MM-DD)
    flr_nm     TEXT,               -- 제출인
    rm         TEXT,               -- 비고(코스피/코스닥 등 — TODO 공식문서 재확인)
    source     TEXT DEFAULT 'dart'
);

-- 수급 이벤트(자사주 매입 기간·실적발표일·콘퍼런스콜 일정). 신호가 아니라 **기록**이다 —
-- analysis/event_study.py가 과거 이벤트 전후를 측정하고, 대시보드 "주시할 이벤트" 카드가
-- 앞으로의 일정을 보여준다. 단일 날짜 이벤트는 end_date = start_date.
-- (ticker, event_type, start_date, end_date)가 같으면 같은 이벤트 — 다시 넣어도 중복되지 않는다.
-- source: 'manual'(scripts/add_event.py) | 'dart'(공시에서 추출, analysis/events.extract_buyback_events).
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    event_type  TEXT NOT NULL,        -- 'buyback' | 'earnings' | 'conf_call'
    start_date  TEXT NOT NULL,        -- YYYY-MM-DD
    end_date    TEXT NOT NULL,        -- YYYY-MM-DD, 단일 날짜 이벤트는 start_date와 같다
    title       TEXT,
    note        TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',
    source_ref  TEXT,                 -- DART 접수번호 등
    created_at  TEXT NOT NULL,
    UNIQUE (ticker, event_type, start_date, end_date)
);

CREATE INDEX IF NOT EXISTS idx_signal_values_signal ON signal_values(signal_id, date);
CREATE INDEX IF NOT EXISTS idx_manual_inputs_type ON manual_inputs(input_type, effective_on);

CREATE INDEX IF NOT EXISTS idx_price_date ON daily_price(date);
CREATE INDEX IF NOT EXISTS idx_flow_date  ON investor_flow(date);
CREATE INDEX IF NOT EXISTS idx_news_ticker_date ON news_items(ticker, published_at);
CREATE INDEX IF NOT EXISTS idx_agent_analyses_ticker ON agent_analyses(ticker, run_date);
CREATE INDEX IF NOT EXISTS idx_financials_corp ON financials(corp_code, bsns_year, reprt_code);
CREATE INDEX IF NOT EXISTS idx_disclosures_stock_date ON disclosures(stock_code, rcept_dt);
CREATE INDEX IF NOT EXISTS idx_events_ticker_date ON events(ticker, end_date);

-- 하워드 막스(오크트리) 공개 메모 목록 (collectors/marks_memos.py). 본문은 저장하지 않는다 — 변경 감지용 해시만.
-- summary는 개인 참고용 짧은 요약(원문 인용 없음, 외부 공개 안 함). (url)로 멱등.
CREATE TABLE IF NOT EXISTS memos (
    url          TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    published_on TEXT,                -- YYYY-MM-DD, 모르면 NULL(지어내지 않음)
    body_hash    TEXT,                -- 본문 텍스트 sha256. 본문 자체는 저장 안 함
    collected_on TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'auto',   -- 'auto' | 'manual'
    summary      TEXT,
    summary_by   TEXT                 -- 'manual' | 모델 이름
);

-- 관점별 토론 섀도 기록 (agents/debate.py, scripts/run_debate.py). 매매 결정·위험 점수에 반영하지 않는다.
-- 앞으로만 쌓는다(과거 날짜 실행 기능 없음 — 모델이 과거 결과를 학습했을 수 있어 과거 백테스트는 오염된다).
-- (decision_date, snapshot_hash, model)로 멱등.
CREATE TABLE IF NOT EXISTS debate_shadow (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at         TEXT NOT NULL,
    decision_date  TEXT NOT NULL,     -- 입력 스냅샷의 기준일(000660 마지막 종가일)
    snapshot_hash  TEXT NOT NULL,
    model          TEXT NOT NULL,     -- 요청한 모델
    model_served   TEXT,              -- 응답이 보고한 모델(거절 시 대체 모델일 수 있음)
    opinions       TEXT NOT NULL,     -- JSON {round1:{관점:의견}, round2:{관점:반박}}
    conclusion     TEXT NOT NULL CHECK (conclusion IN ('위험 높음', '중립', '위험 낮음')),
    evidence       TEXT NOT NULL,     -- JSON [스냅샷 키, ...]
    synthesis      TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    guardrail      TEXT NOT NULL,     -- JSON {passed: true, checked: [...]}
    memo_url       TEXT,
    UNIQUE (decision_date, snapshot_hash, model)
);

-- 가드레일에 걸려 debate_shadow에 저장하지 않은 토론 (append-only, 사람 검토 대기). 채점에 쓰지 않는다.
CREATE TABLE IF NOT EXISTS debate_review (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at         TEXT NOT NULL,
    decision_date  TEXT NOT NULL,
    snapshot_hash  TEXT NOT NULL,
    model          TEXT NOT NULL,
    payload        TEXT NOT NULL,     -- JSON(의견·종합 원문 그대로)
    warnings       TEXT NOT NULL,     -- JSON [...]
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0
);
