"""analysis/checklist.py, analysis/calendar_rules.THEMATIC_WATCHLIST 형태 확인."""

from __future__ import annotations

from analysis import calendar_rules as cal
from analysis import checklist


class TestChecklist:
    def test_every_item_has_text_and_expert(self):
        assert len(checklist.CHECKLIST) == 13
        for item in checklist.CHECKLIST:
            assert item["text"]
            assert item["expert_id"]

    def test_no_forecast_language(self):
        blob = " ".join(item["text"] for item in checklist.CHECKLIST)
        for phrase in ["오를 것", "매수 시점", "매도 시점", "목표가"]:
            assert phrase not in blob


class TestThematicWatchlist:
    def test_every_item_has_label(self):
        for item in cal.THEMATIC_WATCHLIST:
            assert item["label"]

    def test_no_forecast_language(self):
        blob = " ".join(item["label"] + (item.get("note") or "") for item in cal.THEMATIC_WATCHLIST)
        for phrase in ["오를 것", "매수하세요", "매도하세요"]:
            assert phrase not in blob
