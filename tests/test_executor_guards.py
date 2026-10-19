"""
executor 정적 가드(코드를 실행하지 않고 소스를 검사한다).

  1. 의존 방향: baseline 코드(analysis·db·collectors·web·scripts·mcp_server·agents·config.py)는 executor를
     import하지 않는다. executor가 쓰는 baseline 모듈은 analysis·db뿐이고, db.store는 조회 함수만 부른다.
  2. 자금 이동 없음: executor 코드에 이체·출금·입금 관련 식별자나 그런 엔드포인트 문자열이 없다.
  3. append-only: executor 코드가 append-only 테이블을 UPDATE·DELETE하지 않는다.
  4. 브로커 인터페이스는 다섯 메서드뿐이다.
  5. config/ 디렉터리(YAML)가 루트 config.py를 가리지 않는다.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASELINE_DIRS = ["analysis", "db", "collectors", "web", "scripts", "mcp_server", "agents"]
EXECUTOR_DIR = ROOT / "executor"


def _py_files(paths):
    for p in paths:
        p = ROOT / p
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (f for f in p.rglob("*.py") if "__pycache__" not in f.parts)


def _imported_modules(tree: ast.AST) -> set[str]:
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            mods.update(f"{node.module}.{a.name}" for a in node.names)
    return mods


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                ids.add(id(body[0].value))
    return ids


class TestDependencyDirection:
    @pytest.mark.parametrize("path", list(_py_files(BASELINE_DIRS + ["config.py"])), ids=lambda p: str(p.relative_to(ROOT)))
    def test_baseline_does_not_import_executor(self, path):
        mods = _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
        assert not [m for m in mods if m == "executor" or m.startswith("executor.")], path

    def test_executor_imports_only_analysis_and_db_from_baseline(self):
        baseline_top = set(BASELINE_DIRS) | {"config"}
        for f in _py_files(["executor"]):
            mods = _imported_modules(ast.parse(f.read_text(encoding="utf-8")))
            used = {m.split(".")[0] for m in mods} & baseline_top
            assert used <= {"analysis", "db"}, (f, used)

    def test_executor_calls_only_read_functions_of_baseline_store(self):
        write_like = re.compile(r"^(upsert_|insert_|grade_|log_run|init_db|add_|delete|update)")
        for f in _py_files(["executor"]):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "baseline_store"):
                    assert not write_like.match(node.attr), (f, node.attr)


FORBIDDEN = re.compile(r"transfer|withdraw|remit|deposit|이체|출금|입금|송금", re.IGNORECASE)


class TestNoMoneyMovement:
    def test_no_transfer_identifiers_or_endpoints(self):
        """식별자(함수·변수·속성·인자 이름)와 경로·URL처럼 생긴 문자열만 검사한다(설명 문장은 제외)."""
        hits = []
        for f in _py_files(["executor"]):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            docs = _docstring_nodes(tree)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Name):
                    names.append(node.id)
                elif isinstance(node, ast.Attribute):
                    names.append(node.attr)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.append(node.name)
                elif isinstance(node, ast.arg):
                    names.append(node.arg)
                elif isinstance(node, ast.keyword) and node.arg:
                    names.append(node.arg)
                elif (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
                      and ("/" in node.value or "http" in node.value)):
                    names.append(node.value)
                hits += [(f.name, n) for n in names if FORBIDDEN.search(n)]
        assert not hits, hits

    def test_detector_catches_a_planted_transfer_call(self):
        """음성 대조: 검사기가 실제로 잡는지."""
        tree = ast.parse("def x(b):\n    b.transfer_funds(1)\n    requests.post('/uapi/withdraw')\n")
        found = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and FORBIDDEN.search(n.attr)]
        found += [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and FORBIDDEN.search(n.value)]
        assert found == ["transfer_funds", "/uapi/withdraw"]

    def test_broker_interface_has_exactly_six_methods(self):
        # 2026-10-19: 재시작 복구용 조회 메서드 list_orders 추가(조회 전용). 자금 이동 메서드는 여전히 없다.
        from executor.broker.base import Broker
        assert sorted(Broker.__abstractmethods__) == sorted(
            ["get_balance", "get_positions", "place_order", "cancel_order", "get_order_status", "list_orders"])


APPEND_ONLY = ["tier_history", "sleeve_ledger", "exec_events", "promotion_results", "config_history",
               "contributions", "savings_valuations"]


def test_append_only_tables_are_never_updated_or_deleted():
    pat = re.compile(r"\b(UPDATE|DELETE\s+FROM)\s+(" + "|".join(APPEND_ONLY) + r")\b", re.IGNORECASE)
    for f in _py_files(["executor"]):
        assert not pat.search(f.read_text(encoding="utf-8")), f


def test_config_dir_does_not_shadow_config_module():
    import importlib
    import config
    importlib.reload(config)
    assert Path(config.__file__).name == "config.py" and hasattr(config, "settings")
    assert not (ROOT / "config" / "__init__.py").exists()


ORDER_ENDPOINT = re.compile(r"order-cash|order-rvsecncl|order-resv|order-credit|/trading/order")
LIVE_ORDER_TR = re.compile(r"\bTTTC0(011|012|013|802|801|803)U\b")


class TestOrderEndpointsOnlyInBroker:
    """주문 엔드포인트 문자열은 executor/broker/에만 있어야 한다(baseline·executor 다른 모듈·스크립트 어디에도 없음)."""

    def _all_code(self):
        for f in ROOT.rglob("*.py"):
            parts = f.relative_to(ROOT).parts
            if parts[0] in (".venv", "venv", "tests") or "__pycache__" in parts:
                continue
            yield f

    def test_order_paths_appear_only_under_executor_broker(self):
        offenders = [str(f.relative_to(ROOT)) for f in self._all_code()
                     if ORDER_ENDPOINT.search(f.read_text(encoding="utf-8"))
                     and f.relative_to(ROOT).parts[:2] != ("executor", "broker")]
        assert not offenders, offenders

    def test_live_order_tr_ids_appear_nowhere(self):
        offenders = [str(f.relative_to(ROOT)) for f in self._all_code() if LIVE_ORDER_TR.search(f.read_text(encoding="utf-8"))]
        assert not offenders, offenders

    def test_kis_broker_has_only_paper_tr_ids(self):
        from executor.broker import kis
        assert all(t.startswith("V") for t in kis.PAPER_TR_IDS)

    def test_detector_catches_planted_endpoint(self):
        assert ORDER_ENDPOINT.search('requests.post(base + "/uapi/domestic-stock/v1/trading/order-cash")')
        assert LIVE_ORDER_TR.search('tr_id = "TTTC0012U"')
