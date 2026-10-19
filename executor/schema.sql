-- executor 전용 DB(executor.db). baseline.db와 분리한다 — baseline 쪽은 이 파일을 모른다.
-- "append-only"라고 적힌 테이블은 INSERT만 한다(UPDATE·DELETE 하는 코드 없음, 테스트로 확인).

-- 자본 사다리 단계 이력 (append-only). 마지막 행의 to_tier가 현재 단계.
CREATE TABLE IF NOT EXISTS tier_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,              -- 기록 시각(ISO)
    effective_on TEXT NOT NULL,              -- 단계 진입일(YYYY-MM-DD) — 단계 내 성과는 이 날부터
    event        TEXT NOT NULL,              -- 'init' | 'promote' | 'demote'
    from_tier    TEXT,
    to_tier      TEXT NOT NULL,
    operator     TEXT NOT NULL,              -- 'manual' | 'auto'
    reason       TEXT
);

-- 운용 자금(슬리브) 원장 (append-only). account: 'virtual'(shadow·paper) | 'real'.
-- amount_krw는 슬리브 현금에 대한 부호 있는 변화(매수 −, 매도 +, 배정 +, 수수료 −).
-- qty는 보유 수량 변화(매수 +, 매도 −). 슬리브 = 이 원장으로 계산한 현금 + 보유 평가액.
CREATE TABLE IF NOT EXISTS sleeve_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    date       TEXT NOT NULL,
    account    TEXT NOT NULL,
    kind       TEXT NOT NULL,                -- 'allocate' | 'buy' | 'sell'
    ticker     TEXT,
    qty        INTEGER NOT NULL DEFAULT 0,
    price      REAL,
    amount_krw REAL NOT NULL,
    order_key  TEXT,
    note       TEXT
);

-- 주문 (멱등 키: strategy|ticker|decision_date|account). 같은 키로 두 번 주문하지 않는다.
CREATE TABLE IF NOT EXISTS orders (
    order_key       TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    decision_date   TEXT NOT NULL,
    account         TEXT NOT NULL,
    tier            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,           -- 'buy' | 'sell'
    qty             INTEGER NOT NULL,
    ref_price       REAL NOT NULL,
    broker_order_id TEXT,
    -- 'planned'(결정만) → 'sending'(전송 직전) → 'submitted'(브로커 접수, 결과 미확정) → 끝난 상태
    -- 'filled' | 'partial'(잔량 취소 확인) | 'cancelled' | 'rejected' | 'error' | 'expired' | 'skipped_cash' | 'not_sent'.
    -- 'sending'·'submitted'로 남은 행은 재시작 복구(executor/recovery.py)가 브로커 조회로 확정한다.
    status          TEXT NOT NULL,
    filled_qty      INTEGER NOT NULL DEFAULT 0
);

-- 주문·대사 사건 로그 (append-only). 강등 조건의 "주문 오류·잔고 불일치" 건수는 여기서 센다.
CREATE TABLE IF NOT EXISTS exec_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    date      TEXT NOT NULL,
    account   TEXT NOT NULL,
    kind      TEXT NOT NULL,                 -- 'order_error' | 'order_rejected' | 'reconcile_mismatch' | 'cap_rejected' | 'partial_fill' | 'halt' | 'resume'
    order_key TEXT,
    message   TEXT
);

-- 일별 스냅샷 (date, account)로 멱등. reference_value는 비교 기준(백테스트 등) 자산 — 없으면 NULL.
CREATE TABLE IF NOT EXISTS snapshots (
    date              TEXT NOT NULL,
    account           TEXT NOT NULL,
    tier              TEXT NOT NULL,
    account_value     REAL,                  -- 브로커 계좌 평가금액(가상 계좌는 슬리브와 같음)
    sleeve_value      REAL NOT NULL,         -- executor 운용분 평가금액
    operating_capital REAL NOT NULL,         -- 주문 계산에 쓴 자본 = min(슬리브, 단계 한도)
    outside_cash      REAL,                  -- 한도 밖 현금 = 계좌 평가금액 − 운용 자본
    reference_value   REAL,
    recorded_at       TEXT NOT NULL,
    PRIMARY KEY (date, account)
);

-- 승격 리포트 결과 (append-only). promote_tier는 이 기록만 믿지 않고 승격 시점에 다시 계산한다.
CREATE TABLE IF NOT EXISTS promotion_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    tier        TEXT NOT NULL,
    next_tier   TEXT,
    eligible    INTEGER NOT NULL,            -- 1 = 승격 가능(모든 항목 PASS + 기준·다음 단계 확정)
    details     TEXT NOT NULL                -- JSON
);

-- 설정 파일 변경 이력 (append-only). 내용 해시가 직전 기록과 다를 때만 한 행.
CREATE TABLE IF NOT EXISTS config_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    content     TEXT NOT NULL
);

-- 적립(납입) 기록 (append-only, 수동 입력). executor는 돈을 옮기지 않는다 — 사용자가 옮긴 사실만 적는다.
CREATE TABLE IF NOT EXISTS contributions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    date        TEXT NOT NULL,
    amount_krw  REAL NOT NULL,
    destination TEXT NOT NULL,               -- 'savings'(적립 계좌) | 'trading'(자동매매 계좌)
    note        TEXT
);

-- 적립 계좌 평가금액 (append-only, 수동 입력). 없으면 화면에 "데이터 없음".
CREATE TABLE IF NOT EXISTS savings_valuations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    as_of     TEXT NOT NULL,
    value_krw REAL NOT NULL,
    note      TEXT
);

-- 엔진 실행 결과 (append-only). 연속 cap_rejected 횟수를 여기서 센다.
CREATE TABLE IF NOT EXISTS engine_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    date    TEXT NOT NULL,
    account TEXT NOT NULL,
    status  TEXT NOT NULL,
    message TEXT
);

-- 킬 스위치 (append-only). 마지막 행이 'on'이면 전체 주문 중단. 파일 플래그(config/executor.yaml)와 둘 중 하나만 켜져도 중단.
CREATE TABLE IF NOT EXISTS kill_switch_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    state  TEXT NOT NULL,                    -- 'on' | 'off'
    reason TEXT NOT NULL
);

-- 섀도 실행 시도 (append-only). 실행하지 않은 날도 사유와 함께 남는다.
CREATE TABLE IF NOT EXISTS shadow_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    status        TEXT NOT NULL,             -- 'ok' | 'skipped_holiday' | 'skipped_stale' | 'skipped_calendar' | 'blocked_kill_switch' | 'blocked_frozen' | 'error'
    reason        TEXT,
    risk_score    REAL,
    stage         INTEGER,
    target_weight REAL,
    capital       REAL
);

-- 섀도가 "보냈을 주문". (decision_date, ticker)로 멱등 — 같은 날 다시 돌려도 한 번만.
CREATE TABLE IF NOT EXISTS orders_shadow (
    decision_date TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    ref_price     REAL NOT NULL,
    target_weight REAL NOT NULL,
    risk_score    REAL,
    stage         INTEGER,
    recorded_at   TEXT NOT NULL,
    PRIMARY KEY (decision_date, ticker)
);

-- 체결까지 끝난 목표 비중 (append-only). analysis/position_policy의 "목표가 바뀔 때만 다시 계산" 규칙이 직전 목표를
-- 알아야 해서 남긴다. 계획한 주문이 전부 체결됐을 때(또는 주문이 필요 없었을 때)만 기록한다 — 부분 체결·오류·거부가
-- 있었으면 기록하지 않아서, 다음 실행이 목표 수량을 다시 계산한다.
CREATE TABLE IF NOT EXISTS target_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at   TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    account       TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    target_weight REAL NOT NULL
);

-- t일 종가 결정 → t+1일 시가 체결(fill=next_open)에서, 결정 때의 전체 목표 비중을 체결 전까지 보관한다.
-- 체결이 끝나면(filled) target_history에 "체결 완료 목표"로 옮겨 적는다. (decision_date, account)로 멱등.
CREATE TABLE IF NOT EXISTS pending_decisions (
    decision_date TEXT NOT NULL,
    account       TEXT NOT NULL,
    targets       TEXT NOT NULL,             -- JSON {종목: 목표 비중}
    status        TEXT NOT NULL,             -- 'pending' | 'filled' | 'incomplete' | 'expired'
    recorded_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (decision_date, account)
);

-- 섀도: 결정일 종가 vs 다음 거래일 시가 갭(매일, 주문 유무와 무관). 갭 위험 측정용. (decision_date, ticker)로 멱등.
CREATE TABLE IF NOT EXISTS shadow_gaps (
    decision_date  TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    decision_close REAL NOT NULL,
    fill_date      TEXT NOT NULL,
    fill_open      REAL NOT NULL,
    gap_pct        REAL NOT NULL,
    recorded_at    TEXT NOT NULL,
    PRIMARY KEY (decision_date, ticker)
);
