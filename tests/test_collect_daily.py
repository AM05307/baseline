"""
collectors/collect_daily.py 테스트.

실제 yfinance 호출은 절대 하지 않는다 — us_market.collect_all/kr_market.collect_all/
global_markets.collect_all/news.collect_all을 각각 monkeypatch로 가짜 함수로
바꿔치기해서 오케스트레이션 로직(순차 실행·독립적 실패·collect_log 기록·종료
코드·--only·--dry-run)만 검증한다.

3단계(수집 진입점 통합)에서 바뀐 점: 기존엔 us_market·kr_market·news 3개만
묶었는데, global_markets.py(해외 보조 지표 수집기)가 아직 collect_daily에
안 묶여 있었다(CLAUDE.md "아직 안 한 것"에 적혀 있던 항목). 이번에
global_markets를 네 번째 job으로 추가했다 — us_market은 요청 목록(kr_market·
global_markets·news)에 없었지만, 이미 동작하던 수집기를 근거 없이 빼는 건
"값을 지어내지 마/기존 테스트를 삭제해서 통과시키지 마"라는 공통 규칙의
취지에 어긋난다고 판단해 그대로 유지했다. 그래서 기존 테스트들은 job이
4개가 됐다는 전제로 고쳤다(의도는 그대로: 순차 실행·독립 실패·로그 기록
검증) — global_markets도 항상 함께 monkeypatch한다.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors import collect_daily, dart_client, global_markets, kr_market, news, us_market  # noqa: E402
from db import store  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


class TestCollectDailyAllSucceed:
    def test_runs_all_four_and_logs_each(self, tmp_db, monkeypatch):
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 10)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 3)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 5)
        monkeypatch.setattr(news, "collect_all", lambda: 7)

        results = collect_daily.collect_daily(period="1y")

        assert {(r[0], r[1], r[2]) for r in results} == {
            ("us_market", True, 10), ("kr_market", True, 3),
            ("global_markets", True, 5), ("news", True, 7),
        }

        logs = store.recent_logs(10, path=tmp_db)
        assert len(logs) == 4
        assert set(logs["job"]) == {"us_market", "kr_market", "global_markets", "news"}
        assert all(logs["status"] == "ok")

    def test_default_order_is_us_kr_global_news(self, tmp_db, monkeypatch):
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(news, "collect_all", lambda: 1)

        results = collect_daily.collect_daily(period="1y")
        assert [r[0] for r in results] == ["us_market", "kr_market", "global_markets", "news"]


class TestCollectDailyPartialFailureIsolated:
    def test_one_failure_does_not_block_others(self, tmp_db, monkeypatch):
        def failing_news():
            raise RuntimeError("news API 일시 차단")

        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 10)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 3)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 5)
        monkeypatch.setattr(news, "collect_all", failing_news)

        results = collect_daily.collect_daily(period="1y")
        by_job = {r[0]: r for r in results}

        assert by_job["us_market"][1] is True and by_job["us_market"][2] == 10
        assert by_job["kr_market"][1] is True and by_job["kr_market"][2] == 3
        assert by_job["global_markets"][1] is True and by_job["global_markets"][2] == 5
        assert by_job["news"][1] is False
        assert "news API 일시 차단" in by_job["news"][3]

        logs = store.recent_logs(10, path=tmp_db)
        assert len(logs) == 4
        status_by_job = dict(zip(logs["job"], logs["status"]))
        assert status_by_job["us_market"] == "ok"
        assert status_by_job["kr_market"] == "ok"
        assert status_by_job["global_markets"] == "ok"
        assert status_by_job["news"] == "fail"

    def test_no_forecast_language_in_summary(self, tmp_db, monkeypatch, capsys):
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 10)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 3)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 5)
        monkeypatch.setattr(news, "collect_all", lambda: (_ for _ in ()).throw(RuntimeError("차단")))
        monkeypatch.setattr(sys, "argv", ["collect_daily.py"])

        sys.modules.pop("collectors.collect_daily", None)  # runpy가 새로 실행하도록 캐시 제거
        with pytest.raises(SystemExit):
            runpy.run_module("collectors.collect_daily", run_name="__main__")

        out = capsys.readouterr().out
        for phrase in ["오를 것", "매수 시점", "매도 시점"]:
            assert phrase not in out


class TestExitCode:
    def test_exit_zero_when_all_succeed(self, tmp_db, monkeypatch):
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 10)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 3)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 5)
        monkeypatch.setattr(news, "collect_all", lambda: 7)
        monkeypatch.setattr(sys, "argv", ["collect_daily.py"])

        sys.modules.pop("collectors.collect_daily", None)
        try:
            runpy.run_module("collectors.collect_daily", run_name="__main__")
        except SystemExit as e:
            assert e.code in (0, None)

    def test_nonzero_exit_when_one_fails(self, tmp_db, monkeypatch):
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 10)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 3)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 5)

        def failing_news():
            raise RuntimeError("news API 일시 차단")

        monkeypatch.setattr(news, "collect_all", failing_news)
        monkeypatch.setattr(sys, "argv", ["collect_daily.py"])

        sys.modules.pop("collectors.collect_daily", None)
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("collectors.collect_daily", run_name="__main__")
        assert exc_info.value.code not in (0, None)

    def test_period_arg_forwarded_to_market_collectors(self, tmp_db, monkeypatch):
        seen = {}

        def fake_us(period="1y"):
            seen["us"] = period
            return 1

        def fake_kr(period="1y"):
            seen["kr"] = period
            return 1

        def fake_global(period="1y"):
            seen["global"] = period
            return 1

        monkeypatch.setattr(us_market, "collect_all", fake_us)
        monkeypatch.setattr(kr_market, "collect_all", fake_kr)
        monkeypatch.setattr(global_markets, "collect_all", fake_global)
        monkeypatch.setattr(news, "collect_all", lambda: 1)

        collect_daily.collect_daily(period="6mo")
        assert seen == {"us": "6mo", "kr": "6mo", "global": "6mo"}


class TestDartExcludedFromDefaultRun:
    def test_dart_not_called_by_default(self, tmp_db, monkeypatch):
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(news, "collect_all", lambda: 1)

        def boom():
            raise AssertionError("dart가 기본 실행 목록에 포함됨 — DART_API_KEY 검증 전까지는 안 됨")

        monkeypatch.setattr(dart_client, "collect_all", boom)

        results = collect_daily.collect_daily(period="1y")
        assert "dart" not in [r[0] for r in results]

    def test_only_dart_runs_it(self, tmp_db, monkeypatch):
        monkeypatch.setattr(dart_client, "collect_all", lambda: 5)
        results = collect_daily.collect_daily(period="1y", only=["dart"])
        assert results == [("dart", True, 5, None)]

    def test_dart_included_in_scan_limits_all_job_names(self, tmp_db):
        assert "dart" in collect_daily.ALL_JOB_KEYS
        assert "dart" not in collect_daily.JOB_ORDER


class TestOnlyOption:
    def test_only_runs_selected_jobs(self, tmp_db, monkeypatch):
        calls = []
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": calls.append("us") or 1)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": calls.append("kr") or 1)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": calls.append("global") or 1)
        monkeypatch.setattr(news, "collect_all", lambda: calls.append("news") or 1)

        results = collect_daily.collect_daily(period="1y", only=["kr", "global", "news"])

        assert calls == ["kr", "global", "news"]  # us_market은 호출 안 됨
        assert [r[0] for r in results] == ["kr_market", "global_markets", "news"]

        logs = store.recent_logs(10, path=tmp_db)
        assert set(logs["job"]) == {"kr_market", "global_markets", "news"}

    def test_only_preserves_given_order(self, tmp_db, monkeypatch):
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(news, "collect_all", lambda: 1)

        results = collect_daily.collect_daily(period="1y", only=["news", "kr"])
        assert [r[0] for r in results] == ["news", "kr_market"]

    def test_unknown_job_raises(self, tmp_db):
        with pytest.raises(ValueError, match="알 수 없는 job"):
            collect_daily.collect_daily(period="1y", only=["kr", "nope"])

    def test_cli_only_flag_parsed_as_comma_list(self, tmp_db, monkeypatch):
        seen = {}
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": seen.setdefault("kr", True) or 1)
        monkeypatch.setattr(news, "collect_all", lambda: seen.setdefault("news", True) or 1)
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": seen.setdefault("us", True) or 1)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": seen.setdefault("global", True) or 1)
        monkeypatch.setattr(sys, "argv", ["collect_daily.py", "--only", "kr,news"])

        sys.modules.pop("collectors.collect_daily", None)
        try:
            runpy.run_module("collectors.collect_daily", run_name="__main__")
        except SystemExit:
            pass

        assert seen == {"kr": True, "news": True}  # us/global은 호출 안 됨


class TestDryRun:
    def test_dry_run_executes_nothing_and_writes_no_log(self, tmp_db, monkeypatch):
        called = []
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": called.append(1) or 1)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": called.append(1) or 1)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": called.append(1) or 1)
        monkeypatch.setattr(news, "collect_all", lambda: called.append(1) or 1)

        results = collect_daily.collect_daily(period="1y", dry_run=True)

        assert called == []  # 아무 수집기도 실제로 호출되지 않음
        assert [r[0] for r in results] == ["us_market", "kr_market", "global_markets", "news"]
        assert all(r[1] is None for r in results)  # 실행 안 됨 표시

        logs = store.recent_logs(10, path=tmp_db)
        assert len(logs) == 0  # collect_log에도 아무 것도 안 남음

    def test_dry_run_respects_only(self, tmp_db):
        results = collect_daily.collect_daily(period="1y", only=["kr", "news"], dry_run=True)
        assert [r[0] for r in results] == ["kr_market", "news"]

    def test_cli_dry_run_flag_prints_plan_without_network_calls(self, tmp_db, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("dry-run인데 실제 수집기가 호출됨")

        monkeypatch.setattr(us_market, "collect_all", boom)
        monkeypatch.setattr(kr_market, "collect_all", boom)
        monkeypatch.setattr(global_markets, "collect_all", boom)
        monkeypatch.setattr(news, "collect_all", boom)
        monkeypatch.setattr(sys, "argv", ["collect_daily.py", "--dry-run"])

        sys.modules.pop("collectors.collect_daily", None)
        runpy.run_module("collectors.collect_daily", run_name="__main__")  # 예외 없이 끝나야 함(수집기 호출 안 됨)

        out = capsys.readouterr().out
        assert "dry-run" in out
        for job in ["us_market", "kr_market", "global_markets", "news"]:
            assert job in out


class TestIdempotentWrites:
    def test_same_date_rerun_does_not_duplicate_price_rows(self, tmp_db, monkeypatch):
        """
        collect_daily 자체는 upsert를 직접 하지 않지만(각 수집기 안에서 함),
        오케스트레이션을 두 번 실행해도(같은 날 재실행 시나리오) 하위 수집기의
        멱등 저장이 그대로 지켜지는지 확인한다 — 가짜 collect_all이 실제
        store.upsert_prices를 호출하게 만들어서 검증.
        """
        fixed_rows = [{
            "ticker": "TEST000", "date": "2026-01-02",
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
            "volume": 1000, "source": "test",
        }]

        def fake_kr_collect(period="1y"):
            return store.upsert_prices(fixed_rows, path=tmp_db)

        monkeypatch.setattr(kr_market, "collect_all", fake_kr_collect)
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 0)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 0)
        monkeypatch.setattr(news, "collect_all", lambda: 0)

        collect_daily.collect_daily(period="1y")
        collect_daily.collect_daily(period="1y")  # 같은 날 재실행

        prices = store.get_prices("TEST000", path=tmp_db)
        assert len(prices) == 1  # 중복 행 없음 (ON CONFLICT 멱등 upsert)

    def test_same_date_rerun_does_not_duplicate_collect_log_count_per_run(self, tmp_db, monkeypatch):
        """
        collect_log는 누적 로그가 목적이라(원칙 6과 무관하게 append-only로
        설계됨, CLAUDE.md 참고) 재실행마다 새 행이 쌓이는 게 정상 동작이다 —
        "중복 없음"이 아니라 "실행 횟수만큼 정확히 쌓이는지"를 확인한다.
        """
        monkeypatch.setattr(us_market, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(kr_market, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(global_markets, "collect_all", lambda period="1y": 1)
        monkeypatch.setattr(news, "collect_all", lambda: 1)

        collect_daily.collect_daily(period="1y")
        collect_daily.collect_daily(period="1y")

        logs = store.recent_logs(20, path=tmp_db)
        assert len(logs) == 8  # 4 job * 2회 실행
