"""
애널리스트/의견 추적(claims 테이블 확장: affiliation·stance) 테스트.

새 CLI 자체(scripts/add_claim.py)는 기존 관행대로 직접 테스트하지 않는다
(tests/test_grade_claims.py 상단 설명 참고 — 얇은 배선). 대신 그 아래
db/store.py 로직(insert_claim의 새 필드, get_latest_claims_by_person)을
검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _claim(tmp_db, **overrides):
    row = {
        "said_on": "2026-01-01", "who": "어느 애널리스트", "ticker": "000660",
        "summary": "요약", "confidence": "news",
    }
    row.update(overrides)
    store.insert_claim(row, path=tmp_db)


class TestInsertClaimNewFields:
    def test_affiliation_and_stance_round_trip(self, tmp_db):
        _claim(tmp_db, affiliation="어느증권", stance="매수")
        df = store.get_latest_claims_by_person("000660", path=tmp_db)
        assert len(df) == 1
        assert df.iloc[0]["affiliation"] == "어느증권"
        assert df.iloc[0]["stance"] == "매수"

    def test_defaults_to_none_when_omitted(self, tmp_db):
        _claim(tmp_db)  # affiliation/stance 생략
        df = store.get_latest_claims_by_person("000660", path=tmp_db)
        assert df.iloc[0]["affiliation"] is None
        assert df.iloc[0]["stance"] is None

    def test_migrate_is_idempotent(self, tmp_db):
        # init_db를 여러 번 불러도(= _migrate 여러 번 실행) 에러 없이 동일해야 한다(원칙 6).
        store.init_db(tmp_db)
        store.init_db(tmp_db)
        _claim(tmp_db, affiliation="X", stance="중립")
        df = store.get_latest_claims_by_person("000660", path=tmp_db)
        assert df.iloc[0]["stance"] == "중립"


class TestGetLatestClaimsByPerson:
    def test_returns_empty_dataframe_when_no_claims(self, tmp_db):
        df = store.get_latest_claims_by_person("000660", path=tmp_db)
        assert df.empty

    def test_only_most_recent_per_person_is_returned(self, tmp_db):
        _claim(tmp_db, who="A", said_on="2026-01-01", summary="옛날 의견", stance="매도")
        _claim(tmp_db, who="A", said_on="2026-06-01", summary="최근 의견", stance="매수")
        _claim(tmp_db, who="B", said_on="2026-03-01", summary="B의 의견", stance="중립")

        df = store.get_latest_claims_by_person("000660", path=tmp_db)
        assert len(df) == 2  # 사람(A,B)당 1건씩

        a_row = df[df["who"] == "A"].iloc[0]
        assert a_row["summary"] == "최근 의견"
        assert a_row["stance"] == "매수"

    def test_different_tickers_are_kept_separate(self, tmp_db):
        _claim(tmp_db, who="A", ticker="000660", said_on="2026-01-01")
        _claim(tmp_db, who="A", ticker="005930", said_on="2026-01-02")
        assert len(store.get_latest_claims_by_person("000660", path=tmp_db)) == 1
        assert len(store.get_latest_claims_by_person("005930", path=tmp_db)) == 1

    def test_whole_row_is_kept_together_even_with_nulls(self, tmp_db):
        # 회귀 테스트: groupby().first()는 컬럼별로 NaN을 건너뛰어 행이 섞일 수
        # 있다 — 오래된 의견엔 target_price가 있고 최신 의견엔 없을 때, 최신
        # 의견의 summary와 오래된 의견의 target_price가 잘못 합쳐지면 안 된다.
        _claim(tmp_db, who="A", said_on="2026-01-01", summary="오래된 의견",
               target_price=500000.0, stance="매수")
        _claim(tmp_db, who="A", said_on="2026-06-01", summary="최신 의견",
               target_price=None, stance="매도")

        df = store.get_latest_claims_by_person("000660", path=tmp_db)
        row = df[df["who"] == "A"].iloc[0]
        assert row["summary"] == "최신 의견"
        assert row["stance"] == "매도"
        assert row["target_price"] is None or row["target_price"] != row["target_price"]  # NaN/None
