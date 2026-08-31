"""Tests for configurable loss milestone (% of entry premium)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.sl_threshold import (
    loss_milestone_config,
    loss_milestone_rs,
    trade_investment_rs,
)


class TestTradeInvestmentRs:
    def test_debit_is_abs_net_credit(self):
        assert trade_investment_rs(entry_net_credit_rs=-9397.5) == 9397.5

    def test_credit_is_positive_net_credit(self):
        assert trade_investment_rs(entry_net_credit_rs=4500.0) == 4500.0


class TestLossMilestoneThreshold:
    def test_disabled_returns_zero(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": False, "pct_of_premium": 25.0}},
        ):
            rs, pct = loss_milestone_rs(investment_rs=10000.0)
            assert rs == 0.0
            assert pct == 25.0

    def test_enabled_computes_pct_of_premium(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 25.0}},
        ):
            rs, pct = loss_milestone_rs(investment_rs=9397.5)
            assert rs == pytest.approx(2349.375, abs=0.01)
            assert pct == 25.0

    def test_legacy_pct_of_max_loss_fallback(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_max_loss": 20.0}},
        ):
            cfg = loss_milestone_config()
            assert cfg["pct_of_premium"] == 20.0
            rs, pct = loss_milestone_rs(investment_rs=5000.0)
            assert rs == 1000.0
            assert pct == 20.0

    def test_config_clamps_pct(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 150.0}},
        ):
            cfg = loss_milestone_config()
            assert cfg["pct_of_premium"] == 100.0
