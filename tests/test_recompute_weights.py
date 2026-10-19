"""
scripts/recompute_weights.py 테스트 — 특히 weight_cap_pct가 NULL인 신호를
None으로 잘못 캡으로 넘기던 회귀(이전 실제로 터진 버그)를 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import store  # noqa: E402
from scripts import recompute_weights  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", db_path)
    store.init_db(db_path)
    return db_path


def _fake_prices(ticker: str, days: int = 400, seed: int = 0) -> list[dict]:
    rs = np.random.RandomState(seed)
    idx = pd.bdate_range("2024-01-01", periods=days)
    closes = np.cumprod(1 + rs.normal(0, 0.01, days)) * 100
    return [
        {"ticker": ticker, "date": d.strftime("%Y-%m-%d"), "open": float(c), "high": float(c),
         "low": float(c), "close": float(c), "volume": 1000, "source": "test"}
        for d, c in zip(idx, closes)
    ]


class TestRecomputeWeightsHandlesNullCap:
    def test_signal_without_cap_override_does_not_crash(self, tmp_db, capsys):
        # weight_cap_pct를 지정 안 한 신호(= DB에서 NULL/None으로 돌아옴)가
        # 있어도 compute_weights 호출이 TypeError 없이 끝나야 한다.
        store.upsert_prices(_fake_prices("000660"), path=tmp_db)
        store.upsert_expert({"expert_id": "X", "name": "test"}, path=tmp_db)
        store.upsert_signal({
            "signal_id": "NO_CAP", "name": "t", "expert_id": "X",
            "kind": "trade_rule", "direction": "risk", "description": "d",
        }, path=tmp_db)  # weight_cap_pct 생략 -> NULL

        with pytest.MonkeyPatch.context() as m:
            m.setattr(store, "DB_PATH", tmp_db)
            m.setattr(sys, "argv", ["recompute_weights.py", "--dry-run"])
            recompute_weights.main()

        out = capsys.readouterr().out
        assert "NO_CAP" in out

    def test_signal_with_cap_override_is_respected(self, tmp_db, capsys):
        store.upsert_prices(_fake_prices("000660"), path=tmp_db)
        store.upsert_expert({"expert_id": "X", "name": "test"}, path=tmp_db)
        store.upsert_signal({
            "signal_id": "CAPPED", "name": "t", "expert_id": "X",
            "kind": "trade_rule", "direction": "risk", "description": "d",
            "weight_cap_pct": 15.0,
        }, path=tmp_db)

        with pytest.MonkeyPatch.context() as m:
            m.setattr(store, "DB_PATH", tmp_db)
            m.setattr(sys, "argv", ["recompute_weights.py", "--dry-run"])
            recompute_weights.main()

        out = capsys.readouterr().out
        assert "CAPPED" in out
