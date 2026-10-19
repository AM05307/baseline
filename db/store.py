"""
SQLite 저장 계층.

모든 쓰기는 멱등(idempotent)하다. 같은 날 배치를 몇 번 돌려도
행이 중복되지 않는다. 배치 재실행이 안전해야 스케줄러를 믿을 수 있다.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent.parent / "baseline.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"

MIN_SAMPLES_CLAIMS = 30  # 원칙 3. correlation/backtest와 같은 기준값을 따로 둔다(모듈 간 비의존 관행).

# weight_history.algorithm_version — 가중치를 어떤 방식으로 산정했는지(2026-10-18 추가).
# **같은 recomputed_on·같은 signal_id에 행이 여러 개면 algorithm_version이 큰 행, 그다음 id가
# 큰 행을 쓴다**(get_current_weights·get_weight_history 정렬과 analysis/risk_score_history.
# select_weight_rows가 같은 규칙). 행은 지우거나 고치지 않는다(append-only 유지).
WEIGHT_ALGORITHM_VERSIONS = {
    1: "적중률 50% 고정 기준(weighting.compute_weights), 신호값 같은 날짜 정렬",
    2: "기준률 대비 신뢰구간(weighting.compute_weights_significant), 신호값 같은 날짜 정렬",
    3: "2와 같은 공식 + 결정일 정렬(analysis/signal_alignment: 해외 세션 신호는 t일 이전 값, 휴장일 직전 값 유지)",
}
CURRENT_WEIGHT_ALGORITHM_VERSION = 3


@contextmanager
def conn(path: Path | None = None):
    c = sqlite3.connect(path or DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db(path: Path | None = None) -> None:
    with conn(path) as c:
        c.executescript(SCHEMA.read_text())
        _migrate(c)


def _migrate(c: sqlite3.Connection) -> None:
    """
    CREATE TABLE IF NOT EXISTS는 이미 존재하는 테이블에 새 컬럼을 추가해주지 않는다.
    그래서 스키마에 컬럼을 추가할 때마다 여기 안전한 ALTER TABLE을 하나씩 더한다.
    이미 컬럼이 있으면 조용히 넘어간다 — 몇 번을 다시 실행해도 안전해야 한다(원칙 6).
    """
    existing = {row["name"] for row in c.execute("PRAGMA table_info(claims)")}
    if "confidence" not in existing:
        c.execute("ALTER TABLE claims ADD COLUMN confidence TEXT NOT NULL DEFAULT 'news'")
    if "affiliation" not in existing:
        c.execute("ALTER TABLE claims ADD COLUMN affiliation TEXT")
    if "source_ref" not in existing:
        c.execute("ALTER TABLE claims ADD COLUMN source_ref TEXT")  # 영상/회차 등 URL 아닌 출처 표기
    if "date_confidence" not in existing:
        c.execute("ALTER TABLE claims ADD COLUMN date_confidence TEXT NOT NULL DEFAULT 'confirmed'")  # 'confirmed' | 'estimated'
    if "stance" not in existing:
        c.execute("ALTER TABLE claims ADD COLUMN stance TEXT")

    wh_cols = {row["name"] for row in c.execute("PRAGMA table_info(weight_history)")}
    if "source" not in wh_cols:
        # 기존 행은 전부 실시간 재계산 결과였으므로 'live'가 맞다(DEFAULT로 채워짐).
        c.execute("ALTER TABLE weight_history ADD COLUMN source TEXT NOT NULL DEFAULT 'live'")
    if "algorithm_version" not in wh_cols:
        c.execute("ALTER TABLE weight_history ADD COLUMN algorithm_version INTEGER")
    _classify_weight_versions(c)
    # 백필 멱등 키에 버전을 포함한다 — 산정 방식이 바뀐 백필을 기존 행 수정 없이 추가할 수 있게.
    c.execute("DROP INDEX IF EXISTS uq_weight_history_backfill")
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_weight_history_backfill_v "
        "ON weight_history(recomputed_on, signal_id, algorithm_version) WHERE source='backfill'"
    )


def _classify_weight_versions(c: sqlite3.Connection) -> None:
    """
    algorithm_version이 비어 있는 기존 행(컬럼 추가 전 기록)에 버전을 채운다. 값이 비어 있던
    칸을 채우는 것뿐이고 가중치·사유는 건드리지 않는다. 판별 근거는 reason 형식이다:
      - 'in_sample ...'(recompute_weights.py 신 공식) 또는 source='backfill' → 2
      - '20d hit_rate=...'(구 공식) → 1
      - 그 밖('no signal history yet' 등): 같은 날·같은 source 묶음에서 id가 가장 가까운
        판별된 행의 버전(한 번 실행한 재계산은 id가 연속이라). 그것도 없으면 1.
    """
    rows = c.execute(
        "SELECT id, recomputed_on, source, reason FROM weight_history WHERE algorithm_version IS NULL"
    ).fetchall()
    if not rows:
        return
    known: dict[int, int] = {}
    unknown = []
    for r in rows:
        reason = r["reason"] or ""
        if r["source"] == "backfill" or reason.startswith("in_sample"):
            known[r["id"]] = 2
        elif reason.startswith("20d hit_rate"):
            known[r["id"]] = 1
        else:
            unknown.append(r)
    by_batch: dict[tuple, list[tuple[int, int]]] = {}
    for r in rows:
        if r["id"] in known:
            by_batch.setdefault((r["recomputed_on"], r["source"]), []).append((r["id"], known[r["id"]]))
    for r in unknown:
        cands = by_batch.get((r["recomputed_on"], r["source"]), [])
        known[r["id"]] = min(cands, key=lambda x: abs(x[0] - r["id"]))[1] if cands else 1
    c.executemany("UPDATE weight_history SET algorithm_version=? WHERE id=?", [(v, i) for i, v in known.items()])


def upsert_prices(rows: list[dict], path: Path | None = None) -> int:
    """rows: [{ticker,date,open,high,low,close,volume,source}, ...]"""
    if not rows:
        return 0
    sql = """
        INSERT INTO daily_price (ticker,date,open,high,low,close,volume,source)
        VALUES (:ticker,:date,:open,:high,:low,:close,:volume,:source)
        ON CONFLICT(ticker,date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, source=excluded.source
    """
    with conn(path) as c:
        c.executemany(sql, rows)
    return len(rows)


def upsert_flow(rows: list[dict], path: Path | None = None) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO investor_flow (ticker,date,individual,foreigner,institution)
        VALUES (:ticker,:date,:individual,:foreigner,:institution)
        ON CONFLICT(ticker,date) DO UPDATE SET
            individual=excluded.individual,
            foreigner=excluded.foreigner,
            institution=excluded.institution
    """
    with conn(path) as c:
        c.executemany(sql, rows)
    return len(rows)


def upsert_financials(rows: list[dict], path: Path | None = None) -> int:
    """
    rows: [{corp_code, bsns_year, reprt_code, account_nm, fs_div?, sj_div?,
    thstrm_amount?, stock_code?, rcept_no?, rcept_dt}, ...]. (corp_code,
    bsns_year, reprt_code, account_nm)으로 멱등 upsert(원칙 6) — 같은 보고서를
    다시 받아도 행이 늘지 않고 값만 갱신된다. rcept_dt는 미래참조 방지에
    쓰이므로 NOT NULL — 누락되면 호출부(collectors/dart_client.py)가 먼저
    걸러내야 한다.
    """
    if not rows:
        return 0
    sql = """
        INSERT INTO financials (corp_code, bsns_year, reprt_code, account_nm, fs_div, sj_div,
                                 thstrm_amount, stock_code, rcept_no, rcept_dt, source)
        VALUES (:corp_code, :bsns_year, :reprt_code, :account_nm, :fs_div, :sj_div,
                :thstrm_amount, :stock_code, :rcept_no, :rcept_dt, :source)
        ON CONFLICT(corp_code, bsns_year, reprt_code, account_nm) DO UPDATE SET
            fs_div=excluded.fs_div, sj_div=excluded.sj_div,
            thstrm_amount=excluded.thstrm_amount, stock_code=excluded.stock_code,
            rcept_no=excluded.rcept_no, rcept_dt=excluded.rcept_dt
    """
    defaults = {"fs_div": None, "sj_div": None, "thstrm_amount": None,
                "stock_code": None, "rcept_no": None, "source": "dart"}
    payload = [{**defaults, **r} for r in rows]
    with conn(path) as c:
        c.executemany(sql, payload)
    return len(payload)


def financial_report_exists(corp_code: str, bsns_year: str, reprt_code: str, path: Path | None = None) -> bool:
    """이미 이 보고서를 받아뒀는지(멱등 네트워크 호출 생략용 로컬 캐시 판정,
    collectors/dart_client.collect_financials가 쓴다 — "같은 보고서는 다시
    받지 않는다")."""
    with conn(path) as c:
        row = c.execute(
            "SELECT 1 FROM financials WHERE corp_code=? AND bsns_year=? AND reprt_code=? LIMIT 1",
            (corp_code, bsns_year, reprt_code),
        ).fetchone()
    return row is not None


def get_financials(corp_code: str, account_nm: str | None = None, path: Path | None = None) -> pd.DataFrame:
    """corp_code의 저장된 재무계정 전체(또는 account_nm 하나만), rcept_dt 오름차순.
    analysis/dart_financials.py가 이 결과를 받아 as-of 시계열로 바꾼다."""
    with conn(path) as c:
        if account_nm:
            return pd.read_sql_query(
                "SELECT * FROM financials WHERE corp_code=? AND account_nm=? ORDER BY rcept_dt",
                c, params=(corp_code, account_nm), parse_dates=["rcept_dt"],
            )
        return pd.read_sql_query(
            "SELECT * FROM financials WHERE corp_code=? ORDER BY rcept_dt",
            c, params=(corp_code,), parse_dates=["rcept_dt"],
        )


def upsert_disclosures(rows: list[dict], path: Path | None = None) -> int:
    """rows: [{rcept_no, corp_code?, corp_name?, stock_code?, report_nm?,
    rcept_dt, flr_nm?, rm?}, ...]. rcept_no로 멱등 upsert(원칙 6)."""
    if not rows:
        return 0
    sql = """
        INSERT INTO disclosures (rcept_no, corp_code, corp_name, stock_code, report_nm,
                                  rcept_dt, flr_nm, rm, source)
        VALUES (:rcept_no, :corp_code, :corp_name, :stock_code, :report_nm,
                :rcept_dt, :flr_nm, :rm, :source)
        ON CONFLICT(rcept_no) DO UPDATE SET
            corp_code=excluded.corp_code, corp_name=excluded.corp_name,
            stock_code=excluded.stock_code, report_nm=excluded.report_nm,
            rcept_dt=excluded.rcept_dt, flr_nm=excluded.flr_nm, rm=excluded.rm
    """
    defaults = {"corp_code": None, "corp_name": None, "stock_code": None,
                "report_nm": None, "flr_nm": None, "rm": None, "source": "dart"}
    payload = [{**defaults, **r} for r in rows]
    with conn(path) as c:
        c.executemany(sql, payload)
    return len(payload)


def disclosure_exists(rcept_no: str, path: Path | None = None) -> bool:
    with conn(path) as c:
        row = c.execute("SELECT 1 FROM disclosures WHERE rcept_no=? LIMIT 1", (rcept_no,)).fetchone()
    return row is not None


def get_disclosures(stock_code: str | None = None, path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        if stock_code:
            return pd.read_sql_query(
                "SELECT * FROM disclosures WHERE stock_code=? ORDER BY rcept_dt DESC",
                c, params=(stock_code,), parse_dates=["rcept_dt"],
            )
        return pd.read_sql_query("SELECT * FROM disclosures ORDER BY rcept_dt DESC", c, parse_dates=["rcept_dt"])


def get_corp_code_for_stock(stock_code: str, path: Path | None = None) -> str | None:
    """
    이미 수집된 데이터에서(네트워크 호출 없이) 이 종목코드의 DART corp_code를
    찾는다. scripts/compute_signals.py가 쓴다 — 신호 계산 스크립트는 네트워크를
    타지 않는다는 기존 관행을 그대로 지키기 위해, corp_code 조회도 DB에
    이미 저장된 disclosures/financials 행에서만 찾는다. disclosures를
    먼저 본다(공시목록은 항상 corp_code+stock_code를 같이 주는 것으로
    알려져 있어 더 신뢰도가 높음). 둘 다에 없으면 None(지어내지 않음 —
    collectors/dart_client.py를 먼저 실행해야 한다는 뜻).
    """
    with conn(path) as c:
        row = c.execute(
            "SELECT corp_code FROM disclosures WHERE stock_code=? AND corp_code IS NOT NULL LIMIT 1",
            (stock_code,),
        ).fetchone()
        if row:
            return row["corp_code"]
        row = c.execute(
            "SELECT corp_code FROM financials WHERE stock_code=? AND corp_code IS NOT NULL LIMIT 1",
            (stock_code,),
        ).fetchone()
        return row["corp_code"] if row else None


def list_tickers(path: Path | None = None) -> list[str]:
    """daily_price에 있는 모든 티커 목록(6단계: 전체 티커 가격제한 스캔용)."""
    with conn(path) as c:
        df = pd.read_sql_query("SELECT DISTINCT ticker FROM daily_price ORDER BY ticker", c)
    return list(df["ticker"])


def get_prices(ticker: str, path: Path | None = None) -> pd.Series:
    """분석 모듈이 바로 쓸 수 있는 종가 Series (index=날짜)."""
    with conn(path) as c:
        df = pd.read_sql_query(
            "SELECT date, close FROM daily_price WHERE ticker=? ORDER BY date",
            c, params=(ticker,), parse_dates=["date"],
        )
    return df.set_index("date")["close"] if not df.empty else pd.Series(dtype=float)


def get_volume(ticker: str, path: Path | None = None) -> pd.Series:
    """analysis/signals.py의 KHJ_OVERHEAT(거래량 급증 판정)가 쓰는 거래량 Series."""
    with conn(path) as c:
        df = pd.read_sql_query(
            "SELECT date, volume FROM daily_price WHERE ticker=? ORDER BY date",
            c, params=(ticker,), parse_dates=["date"],
        )
    return df.set_index("date")["volume"] if not df.empty else pd.Series(dtype=float)


def get_ohlc(ticker: str, path: Path | None = None) -> pd.DataFrame:
    """
    규칙 백테스트(analysis/rule_backtest.py, analysis/pullback.py)가 체결가로
    쓰는 일봉 OHLC DataFrame.

    get_prices()는 종가만 주는 게 기존 분석 모듈(상관계수 등) 용도에 맞았지만,
    규칙 백테스트는 '전날 종가 신호 → 다음날 시가 체결'을 검증해야 해서 시가가
    필요했다(처음엔 open/close만 있었음). 눌림목 지정가 매매처럼 당일 저가/고가로
    지정가 체결·손절·익절을 판정해야 하는 전략이 추가되면서 high/low도 넣었다 —
    기존 호출부(scripts/backtest_strategies.py 등)는 open/close만 컬럼명으로
    골라 쓰므로 컬럼이 늘어도 영향 없다.
    """
    with conn(path) as c:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close FROM daily_price WHERE ticker=? ORDER BY date",
            c, params=(ticker,), parse_dates=["date"],
        )
    cols = ["open", "high", "low", "close"]
    return df.set_index("date")[cols] if not df.empty else pd.DataFrame(columns=cols)


def get_flow(ticker: str, days: int = 30, path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        return pd.read_sql_query(
            "SELECT * FROM investor_flow WHERE ticker=? ORDER BY date DESC LIMIT ?",
            c, params=(ticker, days), parse_dates=["date"],
        )


def get_foreigner_net(ticker: str, path: Path | None = None) -> pd.Series:
    """
    analysis/signals.ksj_foreign_sell()이 쓰는 외국인 순매수 전체 기간
    Series(오름차순, index=날짜). get_flow()는 대시보드 표시용으로 최근
    N일만 내림차순으로 주는 구조라 rolling 계산에 안 맞아서(모자른 기간은
    rolling 결과가 전부 NaN) get_volume()과 같은 패턴으로 따로 뺐다.
    """
    with conn(path) as c:
        df = pd.read_sql_query(
            "SELECT date, foreigner FROM investor_flow WHERE ticker=? ORDER BY date",
            c, params=(ticker,), parse_dates=["date"],
        )
    return df.set_index("date")["foreigner"] if not df.empty else pd.Series(dtype=float)


def log_run(job: str, status: str, rows: int = 0, message: str = "",
            path: Path | None = None) -> None:
    """배치 실행 기록. 실패를 조용히 넘기지 않기 위한 최소 장치."""
    with conn(path) as c:
        c.execute(
            "INSERT INTO collect_log (ran_at,job,status,rows,message) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), job, status, rows, message),
        )


def recent_logs(limit: int = 10, path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        return pd.read_sql_query(
            "SELECT * FROM collect_log ORDER BY id DESC LIMIT ?", c, params=(limit,)
        )


def upsert_news(rows: list[dict], path: Path | None = None) -> int:
    """
    rows: [{ticker, headline, source, url, published_at, confidence}, ...]

    (ticker, url)로 멱등 처리한다. url이 없는 항목(예: 수동 입력한 소문)은
    매번 새 행으로 들어간다 — 그런 항목은 URL로 중복을 판단할 수 없어서다.
    """
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    sql_with_url = """
        INSERT INTO news_items (ticker,headline,source,url,published_at,confidence,collected_at)
        VALUES (:ticker,:headline,:source,:url,:published_at,:confidence,:collected_at)
        ON CONFLICT(ticker,url) DO UPDATE SET
            headline=excluded.headline, source=excluded.source,
            published_at=excluded.published_at, confidence=excluded.confidence
    """
    sql_no_url = """
        INSERT INTO news_items (ticker,headline,source,url,published_at,confidence,collected_at)
        VALUES (:ticker,:headline,:source,NULL,:published_at,:confidence,:collected_at)
    """
    with conn(path) as c:
        for r in rows:
            r = {**r, "collected_at": now, "confidence": r.get("confidence", "news")}
            if r.get("url"):
                c.execute(sql_with_url, r)
            else:
                c.execute(sql_no_url, r)
    return len(rows)


def get_news(ticker: str | None = None, limit: int = 20, path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        if ticker:
            return pd.read_sql_query(
                "SELECT * FROM news_items WHERE ticker=? ORDER BY published_at DESC LIMIT ?",
                c, params=(ticker, limit),
            )
        return pd.read_sql_query(
            "SELECT * FROM news_items ORDER BY published_at DESC LIMIT ?", c, params=(limit,)
        )


def upsert_agent_analysis(row: dict, path: Path | None = None) -> None:
    """
    row: {ticker, run_date, model, narrative, tool_calls, warnings}

    tool_calls/warnings는 이미 JSON 문자열로 직렬화해서 넘겨야 한다 — 이 계층은
    직렬화 방식을 모른다(순수 저장만). (ticker, run_date)로 멱등 처리한다:
    같은 날 배치를 다시 돌리면 새 행이 아니라 갱신이 된다(원칙 6).
    """
    sql = """
        INSERT INTO agent_analyses (ticker, run_date, model, narrative, tool_calls, warnings, created_at)
        VALUES (:ticker, :run_date, :model, :narrative, :tool_calls, :warnings, :created_at)
        ON CONFLICT(ticker, run_date) DO UPDATE SET
            model=excluded.model, narrative=excluded.narrative,
            tool_calls=excluded.tool_calls, warnings=excluded.warnings,
            created_at=excluded.created_at
    """
    payload = {"warnings": None, **row, "created_at": datetime.now().isoformat(timespec="seconds")}
    with conn(path) as c:
        c.execute(sql, payload)


def get_latest_agent_analysis(ticker: str, path: Path | None = None) -> dict | None:
    """가장 최근 run_date의 캐싱된 에이전트 분석 한 건. 없으면 None."""
    with conn(path) as c:
        row = c.execute(
            "SELECT * FROM agent_analyses WHERE ticker=? ORDER BY run_date DESC, id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    return dict(row) if row else None


def insert_claim(row: dict, path: Path | None = None) -> None:
    """
    row: {said_on, who, ticker, summary, source_url?, source_ref?, price_then?,
          target_price?, horizon_days?, confidence?, affiliation?, stance?,
          date_confidence?}

    전문가 발언/루머 기록. hit/graded_on/price_after는 나중에 채점할 때 채운다.
    confidence 기본값은 'news' — 'rumor'는 명시적으로 지정해야 한다(원칙: 미확인은
    실수로라도 확인된 뉴스처럼 보이면 안 된다). affiliation(소속)·stance(매수/
    중립/매도)는 분류 라벨일 뿐, 1인칭 발언을 지어내는 게 아니다 — summary는
    여전히 본인이 요약한 한 줄이어야 한다(원문 인용 금지, 원칙 5). source_ref는
    URL이 없는 출처(영상 제목·회차 등)를 적는 자리 — source_url과 둘 다 채울
    수도 있다. date_confidence 기본값은 'confirmed' — 촬영일 등이 추정이면
    'estimated'로 명시한다.
    """
    sql = """
        INSERT INTO claims (said_on, who, ticker, summary, source_url, source_ref,
                             price_then, target_price, horizon_days, confidence,
                             affiliation, stance, date_confidence)
        VALUES (:said_on, :who, :ticker, :summary, :source_url, :source_ref,
                :price_then, :target_price, :horizon_days, :confidence,
                :affiliation, :stance, :date_confidence)
    """
    defaults = {"source_url": None, "source_ref": None, "price_then": None, "target_price": None,
                "horizon_days": None, "confidence": "news",
                "affiliation": None, "stance": None, "date_confidence": "confirmed"}
    with conn(path) as c:
        c.execute(sql, {**defaults, **row})


def claim_source_urls(path: Path | None = None) -> set[str]:
    """
    이미 claims에 기록된 source_url 전체(scripts/seed_disclosure_claims.py가
    같은 공시를 중복으로 다시 기록하지 않기 위한 멱등성 체크용). claims
    테이블 자체는 append-only라 DB 제약으로 중복을 막지 않으므로, 쓰는
    쪽(스크립트)이 미리 확인해서 건너뛰는 식으로 멱등을 지킨다(원칙 6).
    """
    with conn(path) as c:
        rows = c.execute("SELECT source_url FROM claims WHERE source_url IS NOT NULL").fetchall()
    return {r["source_url"] for r in rows}


def get_claims_for_grading(include_graded: bool = False, path: Path | None = None) -> pd.DataFrame:
    """
    채점 대상 후보: target_price·horizon_days가 둘 다 있는 claims.

    include_graded=False(기본)면 이미 채점된 건(graded_on NOT NULL)은 제외한다 —
    원칙 6(멱등): --force 없이는 재채점하지 않는다. 만기 도래 여부(said_on+horizon_days
    가 기준일을 지났는지)는 여기서 판정하지 않는다 — 달력 계산이라 호출부
    (scripts/grade_claims.py)에서 datetime으로 한다.
    """
    where = "target_price IS NOT NULL AND horizon_days IS NOT NULL"
    if not include_graded:
        where += " AND graded_on IS NULL"
    with conn(path) as c:
        return pd.read_sql_query(f"SELECT * FROM claims WHERE {where} ORDER BY said_on", c)


def get_latest_claims_by_person(ticker: str, path: Path | None = None) -> pd.DataFrame:
    """
    종목별로 사람(who)당 가장 최근 발언 1건씩. "사람별 최근 의견 + 그 이후
    실제 주가 변화"를 보여주는 대시보드 카드가 쓴다(채점 여부와 무관하게
    보여줄 수 있어야 해서 get_claim_scoreboard와는 별개 — 채점은 horizon_days
    가 지나야 가능하지만, "최근 의견이 뭐였는지"는 바로 보여줄 수 있어야 함).

    가격 변화 계산(price_then 대비 현재가)은 여기서 하지 않는다 — 이 계층은
    조회만 하고, store.get_prices()와 조합하는 건 호출부(web/main.py) 책임이다.
    """
    with conn(path) as c:
        df = pd.read_sql_query(
            "SELECT * FROM claims WHERE ticker=? ORDER BY said_on DESC", c, params=(ticker,),
        )
    if df.empty:
        return df
    # groupby().first()는 컬럼별로 NaN을 건너뛰고 첫 값을 찾아서 행이 섞일 수 있다
    # (예: 최신 발언의 target_price가 NULL이면 더 오래된 발언 값을 끌어옴) —
    # head(1)은 행 전체를 그대로 가져와서 그런 문제가 없다.
    return df.groupby("who", as_index=False).head(1)  # said_on DESC로 이미 정렬돼 있어 첫 행이 최신


def grade_claim(claim_id: int, graded_on: str, price_after: float, hit: int,
                 path: Path | None = None) -> None:
    """
    claim 한 건에 채점 결과를 기록한다. 이미 채점된 걸 다시 채점할지(=--force)는
    호출부가 get_claims_for_grading(include_graded=...)로 대상을 고를 때 이미
    결정한다 — 여기는 무조건 갱신만 한다.
    """
    sql = "UPDATE claims SET graded_on=:graded_on, price_after=:price_after, hit=:hit WHERE id=:id"
    with conn(path) as c:
        c.execute(sql, {"id": claim_id, "graded_on": graded_on,
                         "price_after": price_after, "hit": hit})


def get_claim_scoreboard(recent_limit: int = 10, path: Path | None = None) -> dict:
    """
    채점된 claims의 적중률·표본수·최근 결과 + 미채점 건수. web 대시보드와
    MCP 서버가 공통으로 쓴다(중복 제거). 원칙 3: 표본 수(n)를 항상 함께
    반환하고, MIN_SAMPLES_CLAIMS(30) 미만이면 reliable=False로 표시한다.
    """
    with conn(path) as c:
        graded = pd.read_sql_query(
            "SELECT * FROM claims WHERE graded_on IS NOT NULL "
            "ORDER BY graded_on DESC, id DESC",
            c,
        )
        pending = c.execute(
            "SELECT COUNT(*) FROM claims WHERE graded_on IS NULL "
            "AND target_price IS NOT NULL AND horizon_days IS NOT NULL"
        ).fetchone()[0]
        not_gradable = c.execute(
            "SELECT COUNT(*) FROM claims WHERE graded_on IS NULL "
            "AND (target_price IS NULL OR horizon_days IS NULL)"
        ).fetchone()[0]

    n = len(graded)
    hits = int(graded["hit"].sum()) if n else 0
    recent_df = graded.head(recent_limit)
    recent = (
        recent_df.astype(object).where(recent_df.notna(), None).to_dict("records")
        if not recent_df.empty else []
    )

    return {
        "n": n,
        "hits": hits,
        "hit_rate_pct": round(hits / n * 100, 1) if n else None,
        "reliable": n >= MIN_SAMPLES_CLAIMS,
        "min_samples": MIN_SAMPLES_CLAIMS,
        "pending_count": int(pending),
        "not_gradable_count": int(not_gradable),
        "recent": recent,
    }


def upsert_expert(row: dict, path: Path | None = None) -> None:
    """row: {expert_id, name, title?, source_show?, note?}. (expert_id)로 멱등."""
    sql = """
        INSERT INTO experts (expert_id, name, title, source_show, note)
        VALUES (:expert_id, :name, :title, :source_show, :note)
        ON CONFLICT(expert_id) DO UPDATE SET
            name=excluded.name, title=excluded.title,
            source_show=excluded.source_show, note=excluded.note
    """
    defaults = {"title": None, "source_show": None, "note": None}
    with conn(path) as c:
        c.execute(sql, {**defaults, **row})


def get_experts(path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        return pd.read_sql_query("SELECT * FROM experts ORDER BY expert_id", c)


def upsert_signal(row: dict, path: Path | None = None) -> None:
    """
    row: {signal_id, name, expert_id, kind, description, source_ref?,
          direction?, data_source?, is_manual?, weight_cap_pct?, enabled?}
    (signal_id)로 멱등 — 신호 정의를 다시 등록해도 중복되지 않는다(원칙 6).
    """
    sql = """
        INSERT INTO signals (signal_id, name, expert_id, source_ref, kind, direction,
                              data_source, is_manual, description, weight_cap_pct, enabled)
        VALUES (:signal_id, :name, :expert_id, :source_ref, :kind, :direction,
                :data_source, :is_manual, :description, :weight_cap_pct, :enabled)
        ON CONFLICT(signal_id) DO UPDATE SET
            name=excluded.name, expert_id=excluded.expert_id, source_ref=excluded.source_ref,
            kind=excluded.kind, direction=excluded.direction, data_source=excluded.data_source,
            is_manual=excluded.is_manual, description=excluded.description,
            weight_cap_pct=excluded.weight_cap_pct, enabled=excluded.enabled
    """
    defaults = {
        "source_ref": None, "direction": None, "data_source": None,
        "is_manual": 0, "weight_cap_pct": None, "enabled": 1,
    }
    with conn(path) as c:
        c.execute(sql, {**defaults, **row})


def get_signals(enabled_only: bool = True, path: Path | None = None) -> pd.DataFrame:
    where = "WHERE enabled=1" if enabled_only else ""
    with conn(path) as c:
        return pd.read_sql_query(f"SELECT * FROM signals {where} ORDER BY signal_id", c)


def upsert_signal_values(rows: list[dict], path: Path | None = None) -> int:
    """rows: [{date, signal_id, value, is_on}, ...]. (date, signal_id)로 멱등."""
    if not rows:
        return 0
    sql = """
        INSERT INTO signal_values (date, signal_id, value, is_on)
        VALUES (:date, :signal_id, :value, :is_on)
        ON CONFLICT(date, signal_id) DO UPDATE SET
            value=excluded.value, is_on=excluded.is_on
    """
    with conn(path) as c:
        c.executemany(sql, rows)
    return len(rows)


def get_signal_values(signal_id: str, path: Path | None = None) -> pd.DataFrame:
    """signal_id 하나의 전체 시계열(date, value, is_on). analysis/signal_backtest.py가 쓴다."""
    with conn(path) as c:
        return pd.read_sql_query(
            "SELECT date, value, is_on FROM signal_values WHERE signal_id=? ORDER BY date",
            c, params=(signal_id,), parse_dates=["date"],
        )


def insert_manual_input(row: dict, path: Path | None = None) -> None:
    """
    row: {input_type, effective_on, value_text?, value_num?, note?}

    append-only — 같은 input_type을 다시 넣어도 덮어쓰지 않고 새 행이 쌓인다
    ("입력 이력 남기기"). "지금 유효한 값"은 get_manual_timeline()이 조회
    시점에 effective_on 기준으로 가장 최근 값을 고르는 방식으로 판단한다.
    """
    sql = """
        INSERT INTO manual_inputs (input_type, effective_on, value_text, value_num, note, entered_at)
        VALUES (:input_type, :effective_on, :value_text, :value_num, :note, :entered_at)
    """
    defaults = {"value_text": None, "value_num": None, "note": None}
    payload = {**defaults, **row, "entered_at": datetime.now().isoformat(timespec="seconds")}
    with conn(path) as c:
        c.execute(sql, payload)


DEFAULT_STALE_DAYS = 90  # 5단계 요청: "N일보다 오래되면 stale" 시작 기본값(백테스트로 고르지 않음)


def manual_input_freshness(
    input_type: str, as_of: str | None = None, stale_days: int = DEFAULT_STALE_DAYS,
    path: Path | None = None,
) -> dict:
    """
    input_type의 "as_of 시점에 유효했던"(그 이전 effective_on 중 가장 최근)
    값이 as_of 기준 며칠 지났는지와 stale 여부를 돌려준다(5단계: "manual_inputs
    값이 N일보다 오래되면 stale로 표시"). as_of를 안 주면 오늘 날짜 — 과거
    특정 시점 기준으로도 조회 가능하게 열어둔 이유는 5단계의 "과거 위험
    점수 재구성"이 그 당시 기준으로 수동 입력이 최신이었는지도 같이 봐야
    할 수 있어서다(미래 참조 금지와 같은 이유).

    입력 자체가 없거나 as_of 이전에 유효한 값이 아예 없으면 "모른다"를
    exists=False로 표시하고 stale은 None(지어내지 않음 — stale 여부를
    판단할 기준값 자체가 없다는 뜻).
    """
    as_of_ts = pd.Timestamp(as_of) if as_of else pd.Timestamp.now().normalize()
    timeline = get_manual_timeline(input_type, path=path)
    not_found = {"input_type": input_type, "exists": False, "effective_on": None, "age_days": None, "stale": None}
    if timeline.empty:
        return not_found
    valid = timeline[timeline["effective_on"] <= as_of_ts]
    if valid.empty:
        return not_found
    latest = valid.iloc[-1]
    age_days = (as_of_ts - latest["effective_on"]).days
    return {
        "input_type": input_type, "exists": True,
        "effective_on": latest["effective_on"].date().isoformat(),
        "age_days": int(age_days), "stale": age_days > stale_days,
    }


def get_manual_timeline(input_type: str, path: Path | None = None) -> pd.DataFrame:
    """
    input_type 하나의 전체 타임라인(effective_on 오름차순). 같은 effective_on이
    여러 번 입력됐으면(정정) entered_at이 가장 늦은 것을 그 날짜의 값으로 쓴다.
    analysis/signals.py의 수동 입력 의존 신호가 이 결과를 날짜 인덱스에
    forward-fill해서 쓴다(이 함수 자체는 그 조합을 하지 않는다 — 조회만).
    """
    with conn(path) as c:
        df = pd.read_sql_query(
            "SELECT * FROM manual_inputs WHERE input_type=? ORDER BY effective_on, entered_at",
            c, params=(input_type,), parse_dates=["effective_on"],
        )
    if df.empty:
        return df
    return df.groupby("effective_on", as_index=False).tail(1).sort_values("effective_on")


def insert_weight_history(rows: list[dict], path: Path | None = None) -> int:
    """
    rows: [{recomputed_on, signal_id, old_weight_pct, new_weight_pct, reason, source?, algorithm_version?}, ...]
    algorithm_version을 안 주면 CURRENT_WEIGHT_ALGORITHM_VERSION.
    append-only(가중치는 "현재 상태 저장"이 아니라 "변경 기록"이 목적). source 기본값 'live'.
    백필 행(source='backfill')은 (recomputed_on, signal_id, algorithm_version)이 이미 있으면 **조용히 건너뛴다**
    (기존 행을 수정하지 않는다 — append-only와 멱등을 동시에 지킨다). 충돌 대상을 그 부분
    인덱스로 한정해서 NOT NULL 위반 같은 다른 제약 오류는 예전처럼 예외로 올라온다(OR IGNORE는
    그것까지 삼킨다). 실제로 삽입된 행 수를 돌려준다.
    """
    if not rows:
        return 0
    sql = """
        INSERT INTO weight_history
            (recomputed_on, signal_id, old_weight_pct, new_weight_pct, reason, source, algorithm_version)
        VALUES (:recomputed_on, :signal_id, :old_weight_pct, :new_weight_pct, :reason, :source, :algorithm_version)
        ON CONFLICT (recomputed_on, signal_id, algorithm_version) WHERE source='backfill' DO NOTHING
    """
    payload = [{"source": "live", "algorithm_version": CURRENT_WEIGHT_ALGORITHM_VERSION, **r} for r in rows]
    with conn(path) as c:
        before = c.total_changes
        c.executemany(sql, payload)
        return c.total_changes - before


def get_current_weights(path: Path | None = None) -> dict[str, float]:
    """신호별 가장 최근 new_weight_pct. 이력이 없는 신호는 포함되지 않는다
    (호출부가 기본 동일가중으로 채움 — analysis/weighting.py)."""
    with conn(path) as c:
        df = pd.read_sql_query(
            "SELECT signal_id, new_weight_pct, recomputed_on FROM weight_history "
            "ORDER BY recomputed_on, algorithm_version, id",
            c,
        )
    if df.empty:
        return {}
    latest = df.groupby("signal_id", as_index=False).tail(1)
    return dict(zip(latest["signal_id"], latest["new_weight_pct"]))


def get_weight_history(signal_id: str | None = None, path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        if signal_id:
            return pd.read_sql_query(
                "SELECT * FROM weight_history WHERE signal_id=? ORDER BY recomputed_on, algorithm_version, id", c,
                params=(signal_id,),
            )
        return pd.read_sql_query("SELECT * FROM weight_history ORDER BY recomputed_on, algorithm_version, id", c)


def get_items_on_dates(
    ticker: str, dates: list[str], stock_code: str | None = None, path: Path | None = None,
) -> dict[str, list[dict]]:
    """
    급변일 카드용: 주어진 날짜(YYYY-MM-DD)에 기록된 claim·뉴스·공시를 날짜별로 모은다.
    각 항목 = {kind('claim'|'news'|'disclosure'), text, confidence, source}.

    confidence는 DB에 있는 값 그대로('news'|'rumor') — 공시는 출처가 DART라
    별도 라벨 'disclosure'를 쓴다. 지어내지 않고, 해당 날짜에 행이 없으면
    그 날짜 키 자체가 없다(호출부가 "기록 없음"으로 표시).
    """
    out: dict[str, list[dict]] = {}
    if not dates:
        return out
    marks = ",".join("?" for _ in dates)
    with conn(path) as c:
        for r in c.execute(
            f"SELECT said_on AS d, summary, confidence, who FROM claims "
            f"WHERE ticker=? AND said_on IN ({marks}) ORDER BY id", (ticker, *dates)):
            out.setdefault(r["d"], []).append(
                {"kind": "claim", "text": r["summary"], "confidence": r["confidence"], "source": r["who"]})
        for r in c.execute(
            f"SELECT substr(published_at,1,10) AS d, headline, confidence, source FROM news_items "
            f"WHERE ticker=? AND substr(published_at,1,10) IN ({marks}) ORDER BY id", (ticker, *dates)):
            out.setdefault(r["d"], []).append(
                {"kind": "news", "text": r["headline"], "confidence": r["confidence"], "source": r["source"]})
        if stock_code:
            for r in c.execute(
                f"SELECT rcept_dt AS d, report_nm, corp_name FROM disclosures "
                f"WHERE stock_code=? AND rcept_dt IN ({marks}) ORDER BY rcept_no", (stock_code, *dates)):
                out.setdefault(r["d"], []).append(
                    {"kind": "disclosure", "text": r["report_nm"], "confidence": "disclosure",
                     "source": r["corp_name"] or "DART"})
    return out


def upsert_event(row: dict, path: Path | None = None) -> str:
    """
    row: analysis.events.validate_event()가 돌려주는 모양
         {ticker, event_type, start_date, end_date, title, note, source, source_ref}

    (ticker, event_type, start_date, end_date)로 멱등 — 같은 이벤트를 다시 넣어도 행이 늘지 않는다.
    이미 있을 때의 갱신 규칙(사용자 의도 보호):
      - source='manual' 입력은 title·note를 **새 값이 있을 때만** 덮어쓰고, 행의 source를 'manual'로 둔다
        (사람이 직접 확인한 입력이 자동 추출보다 우선).
      - source='dart' 입력은 비어 있는 칸만 채운다(기존 manual 값을 절대 덮어쓰지 않는다).
    반환: 'inserted' | 'updated'.
    """
    from datetime import datetime
    sql = """
        INSERT INTO events (ticker, event_type, start_date, end_date, title, note, source, source_ref, created_at)
        VALUES (:ticker, :event_type, :start_date, :end_date, :title, :note, :source, :source_ref, :created_at)
        ON CONFLICT (ticker, event_type, start_date, end_date) DO UPDATE SET
            title      = CASE WHEN excluded.source = 'manual'
                              THEN COALESCE(excluded.title, events.title)
                              ELSE COALESCE(events.title, excluded.title) END,
            note       = CASE WHEN excluded.source = 'manual'
                              THEN COALESCE(excluded.note, events.note)
                              ELSE COALESCE(events.note, excluded.note) END,
            source_ref = COALESCE(events.source_ref, excluded.source_ref),
            source     = CASE WHEN excluded.source = 'manual' THEN 'manual' ELSE events.source END
    """
    defaults = {"title": None, "note": None, "source": "manual", "source_ref": None}
    payload = {**defaults, **row, "created_at": datetime.now().isoformat(timespec="seconds")}
    with conn(path) as c:
        existed = c.execute(
            "SELECT 1 FROM events WHERE ticker=? AND event_type=? AND start_date=? AND end_date=?",
            (payload["ticker"], payload["event_type"], payload["start_date"], payload["end_date"]),
        ).fetchone() is not None
        c.execute(sql, payload)
    return "updated" if existed else "inserted"


def get_events(
    ticker: str | None = None, event_type: str | None = None, path: Path | None = None,
) -> pd.DataFrame:
    """이벤트 목록(start_date, end_date 오름차순). 날짜는 문자열 그대로(호출부가 필요하면 변환)."""
    where, params = [], []
    if ticker:
        where.append("ticker=?"); params.append(ticker)
    if event_type:
        where.append("event_type=?"); params.append(event_type)
    sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY start_date, end_date, id"
    with conn(path) as c:
        return pd.read_sql_query(sql, c, params=params)


# ---------------------------------------------------------------- 막스 메모 (collectors/marks_memos.py)

def upsert_memo(row: dict, path: Path | None = None) -> bool:
    """(url)로 멱등. 새 메모면 True. 이미 있으면 제목·게시일·본문 해시만 갱신(요약·수집일은 그대로)."""
    full = {"published_on": None, "body_hash": None, "source": "auto",
            "collected_on": datetime.now().date().isoformat(), **row}
    with conn(path) as c:
        existed = c.execute("SELECT 1 FROM memos WHERE url=?", (full["url"],)).fetchone() is not None
        c.execute("""
            INSERT INTO memos (url, title, published_on, body_hash, collected_on, source)
            VALUES (:url, :title, :published_on, :body_hash, :collected_on, :source)
            ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                published_on=COALESCE(excluded.published_on, memos.published_on),
                body_hash=COALESCE(excluded.body_hash, memos.body_hash)
        """, full)
    return not existed


def set_memo_summary(url: str, summary: str, summary_by: str, path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute("UPDATE memos SET summary=?, summary_by=? WHERE url=?", (summary, summary_by, url))


def get_memos(path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        return pd.read_sql_query("SELECT * FROM memos ORDER BY COALESCE(published_on, collected_on) DESC", c)


def latest_memo(path: Path | None = None) -> dict | None:
    with conn(path) as c:
        row = c.execute("SELECT * FROM memos ORDER BY COALESCE(published_on, collected_on) DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 관점별 토론 섀도 (agents/debate.py)

def insert_debate_shadow(row: dict, path: Path | None = None) -> bool:
    """(decision_date, snapshot_hash, model)로 멱등 — 이미 있으면 False(아무것도 안 바꿈)."""
    with conn(path) as c:
        cur = c.execute("""
            INSERT INTO debate_shadow (run_at, decision_date, snapshot_hash, model, model_served, opinions,
                conclusion, evidence, synthesis, input_tokens, output_tokens, guardrail, memo_url)
            VALUES (:run_at, :decision_date, :snapshot_hash, :model, :model_served, :opinions, :conclusion,
                :evidence, :synthesis, :input_tokens, :output_tokens, :guardrail, :memo_url)
            ON CONFLICT(decision_date, snapshot_hash, model) DO NOTHING
        """, {"run_at": datetime.now().isoformat(timespec="seconds"), "model_served": None, "synthesis": None,
              "memo_url": None, "input_tokens": 0, "output_tokens": 0, **row})
        return cur.rowcount == 1


def debate_shadow_exists(decision_date: str, snapshot_hash: str, model: str, path: Path | None = None) -> bool:
    with conn(path) as c:
        return c.execute("SELECT 1 FROM debate_shadow WHERE decision_date=? AND snapshot_hash=? AND model=?",
                         (decision_date, snapshot_hash, model)).fetchone() is not None


def insert_debate_review(row: dict, path: Path | None = None) -> None:
    with conn(path) as c:
        c.execute("""
            INSERT INTO debate_review (run_at, decision_date, snapshot_hash, model, payload, warnings,
                input_tokens, output_tokens)
            VALUES (:run_at, :decision_date, :snapshot_hash, :model, :payload, :warnings, :input_tokens, :output_tokens)
        """, {"run_at": datetime.now().isoformat(timespec="seconds"), **row})


def get_debate_shadow(path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        return pd.read_sql_query("SELECT * FROM debate_shadow ORDER BY decision_date, id", c)


def get_debate_review(path: Path | None = None) -> pd.DataFrame:
    with conn(path) as c:
        return pd.read_sql_query("SELECT * FROM debate_review ORDER BY id", c)


def debate_tokens_since(since_iso: str, path: Path | None = None) -> tuple[int, int]:
    """since 이후 토론에 쓴 (입력, 출력) 토큰 — 저장된 것과 검토 대기로 간 것 모두(돈은 둘 다 나갔다)."""
    with conn(path) as c:
        a = c.execute("SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM debate_shadow "
                      "WHERE run_at>=?", (since_iso,)).fetchone()
        b = c.execute("SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) FROM debate_review "
                      "WHERE run_at>=?", (since_iso,)).fetchone()
    return int(a[0] + b[0]), int(a[1] + b[1])
