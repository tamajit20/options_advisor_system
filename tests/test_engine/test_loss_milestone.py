"""Tests for configurable loss milestone (early exit % of max loss)."""
from __future__ import annotations

from unittest.mock import patch

from engine.sl_threshold import loss_milestone_config, loss_milestone_rs


class TestLossMilestoneThreshold:
    def test_disabled_returns_zero(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": False, "pct_of_max_loss": 25.0}},
        ):
            rs, pct = loss_milestone_rs(max_loss_rs=10000.0)
            assert rs == 0.0
            assert pct == 25.0

    def test_enabled_computes_pct_of_max_loss(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_max_loss": 25.0}},
        ):
            rs, pct = loss_milestone_rs(max_loss_rs=10000.0)
            assert rs == 2500.0
            assert pct == 25.0

    def test_config_clamps_pct(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_max_loss": 150.0}},
        ):
            cfg = loss_milestone_config()
            assert cfg["pct_of_max_loss"] == 100.0
