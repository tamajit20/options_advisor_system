"""Unit tests for engine.broken_trade_advisor — partial-fill recovery ranking."""
from __future__ import annotations

import pytest

from engine.broken_trade_advisor import advise, diagnose


class TestDiagnose:
    def test_no_pending_returns_full(self):
        executed = [{"action": "SELL", "strike": 23200, "option_type": "CE"}]
        assert diagnose(executed, []) == "FULL"

    def test_no_executed_returns_none_executed(self):
        pending = [{"action": "BUY", "strike": 23300, "option_type": "CE"}]
        assert diagnose([], pending) == "NONE_EXECUTED"

    def test_short_filled_hedge_missing_paired_broken(self):
        executed = [
            {"action": "SELL", "strike": 23200, "option_type": "CE"},
            {"action": "BUY",  "strike": 22700, "option_type": "PE"},
        ]
        pending = [{"action": "BUY", "strike": 23300, "option_type": "CE"}]
        assert diagnose(executed, pending) == "PAIRED_BROKEN"

    def test_only_short_filled_naked_short(self):
        executed = [{"action": "SELL", "strike": 23200, "option_type": "CE"}]
        pending = [{"action": "BUY", "strike": 23300, "option_type": "CE"}]
        assert diagnose(executed, pending) == "NAKED_SHORT"

    def test_only_long_filled_naked_long(self):
        executed = [{"action": "BUY", "strike": 23300, "option_type": "CE"}]
        pending = [{"action": "SELL", "strike": 23200, "option_type": "CE"}]
        assert diagnose(executed, pending) == "NAKED_LONG"


class TestAdvise:
    def test_naked_short_returns_urgent_close_first(self):
        executed = [{"action": "SELL", "strike": 23200, "option_type": "CE",
                     "fill_price": 50, "lots": 1, "lot_size": 75}]
        pending = [{"action": "BUY", "strike": 23300, "option_type": "CE",
                    "lots": 1, "lot_size": 75}]
        chain = [
            {"strike": 23200, "option_type": "CE", "mid_price": 60},
            {"strike": 23300, "option_type": "CE", "mid_price": 30},
        ]
        opts = advise(state="NAKED_SHORT", executed_legs=executed,
                      not_executed_legs=pending, spot=23000, current_chain=chain)
        assert len(opts) >= 1
        assert opts[0].rank == 1
        assert opts[0].recommended is True
        assert opts[0].time_sensitivity == "URGENT"

    def test_naked_long_recommends_hold(self):
        executed = [{"action": "BUY", "strike": 23300, "option_type": "CE",
                     "fill_price": 30, "lots": 1, "lot_size": 75}]
        pending = [{"action": "SELL", "strike": 23200, "option_type": "CE",
                    "lots": 1, "lot_size": 75}]
        chain = [
            {"strike": 23200, "option_type": "CE", "mid_price": 60},
            {"strike": 23300, "option_type": "CE", "mid_price": 30},
        ]
        opts = advise(state="NAKED_LONG", executed_legs=executed,
                      not_executed_legs=pending, spot=23000, current_chain=chain)
        assert opts[0].label.lower().startswith("hold")
        assert opts[0].time_sensitivity == "LOW"

    def test_full_or_unknown_returns_empty(self):
        opts = advise(state="FULL", executed_legs=[], not_executed_legs=[],
                      spot=23000, current_chain=[])
        assert opts == []
