"""
scripts/run_weekly.py 테스트.

실제 서브프로세스를 절대 실행하지 않는다 — subprocess.run을 monkeypatch로
가짜 함수로 바꿔치기해서 (1) 명령을 올바른 순서로 만드는지, (2) 중간 단계가
실패하면 체인이 거기서 멈추고 종료 코드가 전달되는지, (3) --dry-run이
아무 서브프로세스도 안 부르는지만 검증한다. 세 하위 스크립트(collect_daily/
compute_signals/recompute_weights) 자체의 동작은 각자의 테스트 파일에서
이미 검증하므로 여기서 다시 검증하지 않는다(이 스크립트는 얇은 배선).
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import run_weekly  # noqa: E402


class TestBuildCommands:
    def test_order_is_collect_compute_weights(self):
        commands = run_weekly.build_commands(period="1y", only=None, min_events=30)
        assert [name for name, _ in commands] == ["collect_daily", "compute_signals", "recompute_weights"]

    def test_period_forwarded_to_collect_daily_only(self):
        commands = run_weekly.build_commands(period="6mo", only=None, min_events=30)
        collect_cmd = dict(commands)["collect_daily"]
        assert "--period" in collect_cmd and "6mo" in collect_cmd
        compute_cmd = dict(commands)["compute_signals"]
        assert "--period" not in compute_cmd  # compute_signals는 period 인자가 없음

    def test_only_forwarded_to_collect_daily(self):
        commands = run_weekly.build_commands(period="1y", only="kr,global,news", min_events=30)
        collect_cmd = dict(commands)["collect_daily"]
        assert "--only" in collect_cmd and "kr,global,news" in collect_cmd

    def test_only_omitted_when_none(self):
        commands = run_weekly.build_commands(period="1y", only=None, min_events=30)
        collect_cmd = dict(commands)["collect_daily"]
        assert "--only" not in collect_cmd

    def test_min_events_forwarded_to_recompute_weights(self):
        commands = run_weekly.build_commands(period="1y", only=None, min_events=20)
        weights_cmd = dict(commands)["recompute_weights"]
        assert "--min-events" in weights_cmd and "20" in weights_cmd


class TestChainExecution:
    def test_runs_all_three_in_order_when_all_succeed(self, monkeypatch):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["run_weekly.py"])

        sys.modules.pop("scripts.run_weekly", None)
        runpy.run_module("scripts.run_weekly", run_name="__main__")

        assert len(calls) == 3
        assert "collect_daily.py" in calls[0][1]
        assert "compute_signals.py" in calls[1][1]
        assert "recompute_weights.py" in calls[2][1]

    def test_stops_and_exits_nonzero_when_first_step_fails(self, monkeypatch):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return SimpleNamespace(returncode=1)  # collect_daily 실패

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["run_weekly.py"])

        sys.modules.pop("scripts.run_weekly", None)
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("scripts.run_weekly", run_name="__main__")

        assert exc_info.value.code == 1
        assert len(calls) == 1  # compute_signals/recompute_weights는 호출 안 됨

    def test_stops_after_second_step_failure_without_running_third(self, monkeypatch):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            ok = len(calls) != 2  # 두 번째 호출(compute_signals)만 실패
            return SimpleNamespace(returncode=0 if ok else 3)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(sys, "argv", ["run_weekly.py"])

        sys.modules.pop("scripts.run_weekly", None)
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("scripts.run_weekly", run_name="__main__")

        assert exc_info.value.code == 3
        assert len(calls) == 2  # recompute_weights는 호출 안 됨


class TestDryRun:
    def test_dry_run_calls_no_subprocess(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("dry-run인데 실제로 서브프로세스가 호출됨")

        monkeypatch.setattr(subprocess, "run", boom)
        monkeypatch.setattr(sys, "argv", ["run_weekly.py", "--dry-run"])

        sys.modules.pop("scripts.run_weekly", None)
        runpy.run_module("scripts.run_weekly", run_name="__main__")  # 예외 없이 끝나야 함

        out = capsys.readouterr().out
        assert "dry-run" in out
        for name in ["collect_daily", "compute_signals", "recompute_weights"]:
            assert name in out


class TestMemosOption:
    def test_only_memos_runs_just_the_memo_collector(self):
        cmds = run_weekly.build_commands("1y", "memos", 30)
        assert [n for n, _ in cmds] == ["marks_memos"] and cmds[0][1][-1].endswith("marks_memos.py")

    def test_memos_with_other_jobs_appends_memo_step_and_strips_it_from_collect_daily(self):
        cmds = dict(run_weekly.build_commands("1y", "kr,memos", 30))
        assert list(cmds) == ["collect_daily", "compute_signals", "recompute_weights", "marks_memos"]
        assert cmds["collect_daily"][-2:] == ["--only", "kr"]

    def test_default_chain_does_not_include_memos(self):
        assert "marks_memos" not in dict(run_weekly.build_commands("1y", None, 30))
