"""executor 자본 사다리 순수 로직: 단계 파싱·운용 자본·강등 판정·승격 기준·설정 변경 이력."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from executor import config_files, store  # noqa: E402
from executor.criteria import promotion_status  # noqa: E402
from executor.tiers import DemotionRule, TierMetrics, check_demotion, operating_capital, parse_ladder  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _m(**kw) -> TierMetrics:
    base = dict(entry_date="2026-01-01", last_date="2026-06-01", days=100, trades=30, cum_return_pct=5.0,
                mdd_pct=-5.0, divergence_pct=1.0, order_errors=0, reconciliation_mismatches=0,
                bh_return_pct=3.0, bh_mdd_pct=-10.0)
    base.update(kw)
    return TierMetrics(**base)


class TestShippedConfig:
    def test_shipped_files_parse_and_unconfirmed_are_marked(self):
        ladder = parse_ladder(yaml.safe_load((ROOT / "config/capital_tiers.example.yaml").read_text()))
        assert [t.name for t in ladder.tiers] == ["shadow", "paper", "live_1", "live_2", "live_3"]
        assert ladder.get("live_1").max_capital_krw == 1_000_000 and ladder.get("live_1").promotable_to  # 예시값
        assert not ladder.get("live_2").promotable_to and ladder.get("live_2").max_capital_krw is None
        criteria = yaml.safe_load((ROOT / "config/promotion_criteria.yaml").read_text())
        assert all(not s["confirmed"] for s in criteria["stages"].values())  # 사용자 확정 전

    def test_shipped_criteria_block_every_promotion_even_if_metrics_are_perfect(self):
        ladder = parse_ladder(yaml.safe_load((ROOT / "config/capital_tiers.example.yaml").read_text()))
        criteria = yaml.safe_load((ROOT / "config/promotion_criteria.yaml").read_text())
        perfect = _m(days=10_000, trades=10_000, mdd_pct=0.0, divergence_pct=0.0)
        for t in ladder.tiers:
            assert not promotion_status(ladder, criteria, t.name, perfect).eligible


class TestParse:
    def test_rejects_bad_ladders(self):
        with pytest.raises(ValueError):
            parse_ladder({"tiers": [{"name": "a", "kind": "real", "max_capital_krw": 1}]})  # 첫 단계 real
        with pytest.raises(ValueError):
            parse_ladder({"tiers": [{"name": "a", "kind": "virtual"}, {"name": "a", "kind": "virtual"}]})
        with pytest.raises(ValueError):
            parse_ladder({"tiers": [{"name": "a", "kind": "virtual"}, {"name": "b", "kind": "real"},
                                    {"name": "c", "kind": "virtual"}]})


LADDER = parse_ladder({"tiers": [
    {"name": "shadow", "kind": "virtual", "max_capital_krw": 1_000_000, "confirmed": True, "promotion_criteria": "s"},
    {"name": "live_1", "kind": "real", "max_capital_krw": 2_000_000, "confirmed": True, "promotion_criteria": "l",
     "demotion": {"max_mdd_pct": 15, "max_divergence_pct": 5, "max_order_errors": 0, "max_reconciliation_mismatches": 0}},
    {"name": "live_2", "kind": "real", "max_capital_krw": "미확정", "confirmed": False},
]})


class TestOperatingCapital:
    def test_min_of_sleeve_and_cap(self):
        t = LADDER.get("live_1")
        assert operating_capital(t, "real", 6_000_000, LADDER) == 2_000_000  # 운용분이 커도 한도
        assert operating_capital(t, "real", 1_500_000, LADDER) == 1_500_000  # 손실 후엔 그 금액(채우지 않음)
        assert operating_capital(t, "real", -5, LADDER) == 0

    def test_virtual_tier_never_uses_real_money_and_unconfirmed_cap_is_zero(self):
        assert operating_capital(LADDER.get("shadow"), "real", 9_000_000, LADDER) == 0
        assert operating_capital(LADDER.get("live_1"), "shadow", 9_000_000, LADDER) == 1_000_000  # 섀도는 비교 기준으로 계속
        assert operating_capital(LADDER.get("live_2"), "real", 9_000_000, LADDER) == 0


class TestDemotion:
    RULE = DemotionRule(15, 5, 0, 0, confirmed=False)

    def test_each_condition_triggers(self):
        assert check_demotion(self.RULE, _m(mdd_pct=-15.1))
        assert check_demotion(self.RULE, _m(divergence_pct=5.01))
        assert check_demotion(self.RULE, _m(order_errors=1))
        assert check_demotion(self.RULE, _m(reconciliation_mismatches=1))
        assert check_demotion(self.RULE, _m()) == []

    def test_missing_metric_does_not_trigger_and_no_rule_never_demotes(self):
        assert check_demotion(self.RULE, _m(mdd_pct=None, divergence_pct=None)) == []
        assert check_demotion(None, _m(mdd_pct=-90)) == []


CRIT = {"stages": {
    "s": {"confirmed": True, "min_days": 50, "min_trades": 10, "max_mdd_ratio_vs_buy_hold": 1.0,
          "max_divergence_pct": 2.0, "max_order_errors": 0, "max_reconciliation_mismatches": 0},
    "l": {"confirmed": True, "min_days": 50, "min_trades": 10},
}}


class TestPromotionStatus:
    def test_all_pass_and_confirmed_is_eligible(self):
        st = promotion_status(LADDER, CRIT, "shadow", _m())
        assert st.eligible and st.all_pass and st.next_tier == "live_1"

    def test_failed_item_blocks(self):
        st = promotion_status(LADDER, CRIT, "shadow", _m(trades=3, mdd_pct=-20.0))
        assert not st.eligible
        assert {i.label for i in st.items if not i.passed} == {"거래 수", "MDD / 단순 보유 MDD"}

    def test_missing_data_is_fail_not_pass(self):
        st = promotion_status(LADDER, CRIT, "shadow", _m(divergence_pct=None))
        item = next(i for i in st.items if i.key == "max_divergence_pct")
        assert not item.passed and "데이터 없음" in item.note and not st.eligible

    def test_unconfirmed_criteria_or_value_blocks(self):
        crit = {"stages": {"s": {**CRIT["stages"]["s"], "confirmed": False}}}
        assert any("미확정" in b for b in promotion_status(LADDER, crit, "shadow", _m()).blockers)
        crit = {"stages": {"s": {**CRIT["stages"]["s"], "min_days": "미확정"}}}
        st = promotion_status(LADDER, crit, "shadow", _m())
        assert not st.eligible and next(i for i in st.items if i.key == "min_days").note == "미확정"

    def test_unconfirmed_next_tier_blocks_even_if_all_pass(self):
        st = promotion_status(LADDER, CRIT, "live_1", _m())
        assert st.all_pass and not st.eligible
        assert any("live_2 미확정" in b for b in st.blockers)

    def test_last_tier(self):
        assert not promotion_status(LADDER, CRIT, "live_2", _m()).eligible


class TestConfigHistory:
    def test_records_only_on_change(self, tmp_path):
        db = tmp_path / "e.db"
        store.init_db(db)
        f = tmp_path / "capital_tiers.yaml"
        f.write_text((ROOT / "config/capital_tiers.example.yaml").read_text())
        config_files.load_ladder(f, db)
        config_files.load_ladder(f, db)
        assert len(store.config_history(db)) == 1
        f.write_text(f.read_text().replace("max_mdd_pct: 15.0", "max_mdd_pct: 12.0", 1))
        config_files.load_ladder(f, db)
        h = store.config_history(db)
        assert len(h) == 2 and h["sha256"].nunique() == 2
        config_files.load_ladder(f, db, record=False)  # 화면용 읽기는 기록 안 함
        assert len(store.config_history(db)) == 2


class TestConfigResolution:
    """실제 금액은 *.local.yaml(gitignore), 저장소에는 *.example.yaml — local이 있으면 local, 없으면 example."""

    def test_local_wins_then_example_then_plain(self, tmp_path):
        (tmp_path / "capital_tiers.yaml").write_text("x")
        assert config_files.resolve_config("capital_tiers", tmp_path).name == "capital_tiers.yaml"
        (tmp_path / "capital_tiers.example.yaml").write_text("x")
        p = config_files.resolve_config("capital_tiers", tmp_path)
        assert p.name == "capital_tiers.example.yaml" and config_files.is_example(p)
        (tmp_path / "capital_tiers.local.yaml").write_text("x")
        p = config_files.resolve_config("capital_tiers", tmp_path)
        assert p.name == "capital_tiers.local.yaml" and not config_files.is_example(p)

    def test_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            config_files.resolve_config("capital_tiers", tmp_path)

    def test_repo_ships_example_and_ignores_local(self):
        import subprocess
        assert (ROOT / "config/capital_tiers.example.yaml").exists()
        tracked = subprocess.run(["git", "ls-files", "config"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        assert "config/capital_tiers.example.yaml" in tracked
        assert not [f for f in tracked if f.endswith(".local.yaml")] and "config/capital_tiers.yaml" not in tracked
        ignored = subprocess.run(["git", "check-ignore", "-q", "config/capital_tiers.local.yaml"], cwd=ROOT)
        assert ignored.returncode == 0

    def test_lot_size_table_default_capital_reads_local_then_example(self, tmp_path):
        from scripts import lot_size_table
        (tmp_path / "capital_tiers.example.yaml").write_text("tiers:\n  - {name: live_1, max_capital_krw: 1000000}\n")
        assert lot_size_table.live_1_capital(tmp_path) == (tmp_path / "capital_tiers.example.yaml", 1_000_000.0)
        (tmp_path / "capital_tiers.local.yaml").write_text("tiers:\n  - {name: live_1, max_capital_krw: 미확정}\n")
        assert lot_size_table.live_1_capital(tmp_path) == (tmp_path / "capital_tiers.local.yaml", None)
