"""
scripts/grade_claims.py 테스트.

CLI 자체(argparse/print)는 얇은 배선이라 add_claim.py·run_agents.py처럼 직접
테스트하지 않는다. 대신 채점 판정 로직(_judge)·시세 조회(_price_after)·후보
채점(_grade_candidates)과 db/store.py의 grade_claim/get_claims_for_grading/
get_claim_scoreboard를 조합해서 실제 채점 파이프라인을 검증한다.
"""

from __future__ import annotations

import runpy
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from scripts import grade_claims  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _prices(ticker: str, rows: list[tuple[str, float]]) -> list[dict]:
    return [
        {"ticker": ticker, "date": d, "open": c, "high": c, "low": c, "close": c,
         "volume": 1000, "source": "test"}
        for d, c in rows
    ]


class TestJudge:
    def test_up_direction_hit(self):
        hit, reason = grade_claims._judge(price_then=100, target_price=120, price_after=125)
        assert hit == 1 and reason == "상승 방향"

    def test_up_direction_miss(self):
        hit, reason = grade_claims._judge(price_then=100, target_price=120, price_after=110)
        assert hit == 0 and reason == "상승 방향"

    def test_down_direction_hit(self):
        hit, reason = grade_claims._judge(price_then=100, target_price=80, price_after=75)
        assert hit == 1 and reason == "하락 방향"

    def test_down_direction_miss(self):
        hit, reason = grade_claims._judge(price_then=100, target_price=80, price_after=90)
        assert hit == 0 and reason == "하락 방향"

    def test_no_direction_is_miss(self):
        hit, reason = grade_claims._judge(price_then=100, target_price=100, price_after=100)
        assert hit == 0
        assert "방향 불명" in reason


class TestGradeCandidatesPipeline:
    def test_skips_when_price_data_missing(self, tmp_db):
        """said_on+horizon_days 이후 시세가 DB에 없으면(수집 안 됨) 건너뛴다."""
        store.insert_claim({
            "said_on": "2026-01-01", "who": "테스트", "ticker": "NOPRICE",
            "summary": "요약", "price_then": 100, "target_price": 120,
            "horizon_days": 30, "confidence": "news",
        }, path=tmp_db)
        candidates = store.get_claims_for_grading(path=tmp_db)
        graded, skipped = grade_claims._grade_candidates(candidates, as_of=date(2026, 6, 1))
        assert graded == []
        assert skipped["시세 데이터 없음(수집 안 됨)"] == 1

    def test_skips_when_not_due_yet(self, tmp_db):
        store.insert_claim({
            "said_on": "2026-06-01", "who": "테스트", "ticker": "000660",
            "summary": "요약", "price_then": 100, "target_price": 120,
            "horizon_days": 90, "confidence": "news",
        }, path=tmp_db)
        candidates = store.get_claims_for_grading(path=tmp_db)
        graded, skipped = grade_claims._grade_candidates(candidates, as_of=date(2026, 6, 15))
        assert graded == []
        assert skipped["만기 미도래"] == 1

    def test_grades_up_direction_hit_end_to_end(self, tmp_db):
        store.upsert_prices(
            _prices("000660", [("2026-01-01", 100.0), ("2026-01-31", 130.0), ("2026-02-02", 131.0)]),
            path=tmp_db,
        )
        store.insert_claim({
            "said_on": "2026-01-01", "who": "어느 리포트", "ticker": "000660",
            "summary": "상승 전망", "price_then": 100, "target_price": 120,
            "horizon_days": 30, "confidence": "news",
        }, path=tmp_db)
        candidates = store.get_claims_for_grading(path=tmp_db)
        graded, skipped = grade_claims._grade_candidates(candidates, as_of=date(2026, 2, 1))
        assert not skipped
        assert len(graded) == 1
        g = graded[0]
        assert g["hit"] == 1
        assert g["price_after"] == 130.0
        assert g["graded_on"] == "2026-01-31"  # said_on+30일=01-31, 정확히 그 날 시세 존재

    def test_grades_down_direction_miss_and_writes_via_store(self, tmp_db):
        store.upsert_prices(
            _prices("000660", [("2026-01-01", 100.0), ("2026-01-31", 95.0)]),
            path=tmp_db,
        )
        store.insert_claim({
            "said_on": "2026-01-01", "who": "루머", "ticker": "000660",
            "summary": "하락 전망", "price_then": 100, "target_price": 80,
            "horizon_days": 30, "confidence": "rumor",
        }, path=tmp_db)
        candidates = store.get_claims_for_grading(path=tmp_db)
        graded, _ = grade_claims._grade_candidates(candidates, as_of=date(2026, 2, 1))
        assert len(graded) == 1 and graded[0]["hit"] == 0

        g = graded[0]
        store.grade_claim(g["id"], g["graded_on"], g["price_after"], g["hit"], path=tmp_db)

        board = store.get_claim_scoreboard(path=tmp_db)
        assert board["n"] == 1
        assert board["hits"] == 0
        assert board["hit_rate_pct"] == 0.0
        assert board["reliable"] is False  # n=1 < MIN_SAMPLES_CLAIMS

    def test_uses_next_trading_day_when_due_date_is_not_a_trading_day(self, tmp_db):
        """due_date(01-31) 시세가 없고 그 다음 거래일(02-02)만 있으면 그걸 쓴다."""
        store.upsert_prices(
            _prices("000660", [("2026-01-01", 100.0), ("2026-02-02", 140.0)]),
            path=tmp_db,
        )
        store.insert_claim({
            "said_on": "2026-01-01", "who": "테스트", "ticker": "000660",
            "summary": "요약", "price_then": 100, "target_price": 120,
            "horizon_days": 30, "confidence": "news",
        }, path=tmp_db)
        candidates = store.get_claims_for_grading(path=tmp_db)
        graded, _ = grade_claims._grade_candidates(candidates, as_of=date(2026, 2, 3))
        assert len(graded) == 1
        assert graded[0]["graded_on"] == "2026-02-02"
        assert graded[0]["price_after"] == 140.0


class TestAlreadyGradedNotRegradedWithoutForce:
    def test_default_excludes_already_graded(self, tmp_db):
        store.upsert_prices(
            _prices("000660", [("2026-01-01", 100.0), ("2026-01-31", 130.0)]), path=tmp_db,
        )
        store.insert_claim({
            "said_on": "2026-01-01", "who": "테스트", "ticker": "000660",
            "summary": "요약", "price_then": 100, "target_price": 120,
            "horizon_days": 30, "confidence": "news",
        }, path=tmp_db)
        claim_id = store.get_claims_for_grading(path=tmp_db).iloc[0]["id"]
        store.grade_claim(int(claim_id), "2026-01-31", 130.0, 1, path=tmp_db)

        # --force 없이는 이미 채점된 건이 후보에 아예 안 잡혀야 한다
        candidates = store.get_claims_for_grading(include_graded=False, path=tmp_db)
        assert candidates.empty

    def test_force_includes_already_graded(self, tmp_db):
        store.upsert_prices(
            _prices("000660", [("2026-01-01", 100.0), ("2026-01-31", 130.0)]), path=tmp_db,
        )
        store.insert_claim({
            "said_on": "2026-01-01", "who": "테스트", "ticker": "000660",
            "summary": "요약", "price_then": 100, "target_price": 120,
            "horizon_days": 30, "confidence": "news",
        }, path=tmp_db)
        claim_id = store.get_claims_for_grading(path=tmp_db).iloc[0]["id"]
        store.grade_claim(int(claim_id), "2026-01-31", 130.0, 1, path=tmp_db)

        candidates = store.get_claims_for_grading(include_graded=True, path=tmp_db)
        assert len(candidates) == 1


class TestDryRunDoesNotWrite:
    def test_dry_run_leaves_db_untouched(self, tmp_db, monkeypatch, capsys):
        store.upsert_prices(
            _prices("000660", [("2026-01-01", 100.0), ("2026-01-31", 130.0)]), path=tmp_db,
        )
        store.insert_claim({
            "said_on": "2026-01-01", "who": "테스트", "ticker": "000660",
            "summary": "요약", "price_then": 100, "target_price": 120,
            "horizon_days": 30, "confidence": "news",
        }, path=tmp_db)

        monkeypatch.setattr(sys, "argv", ["grade_claims.py", "--as-of", "2026-02-01", "--dry-run"])
        sys.modules.pop("scripts.grade_claims", None)  # runpy가 새로 실행하도록 캐시 제거
        runpy.run_module("scripts.grade_claims", run_name="__main__")

        board = store.get_claim_scoreboard(path=tmp_db)
        assert board["n"] == 0  # dry-run이므로 DB에 채점 결과가 전혀 반영되지 않아야 한다
        out = capsys.readouterr().out
        assert "dry-run" in out
