"""Tests for configurable loss milestone (% of entry premium)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.sl_threshold import (
    loss_milestone_config,
    loss_milestone_rs,
    profit_milestone_config,
    profit_milestone_line_rs,
    profit_milestone_rs,
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

    def test_auto_close_defaults_true(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert loss_milestone_config()["auto_close"] is True

    def test_auto_close_can_be_disabled(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "auto_close": False,
            }},
        ):
            assert loss_milestone_config()["auto_close"] is False

    def test_confirm_seconds_defaults_to_20(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert loss_milestone_config()["confirm_seconds"] == 20

    def test_confirm_seconds_zero_is_immediate(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "confirm_seconds": 0,
            }},
        ):
            assert loss_milestone_config()["confirm_seconds"] == 0


class TestProfitMilestoneThreshold:
    def test_disabled_returns_zero(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": False, "pct_of_premium": 10.0}},
        ):
            rs, pct = profit_milestone_rs(investment_rs=10000.0)
            assert rs == 0.0
            assert pct == 10.0

    def test_enabled_computes_pct_of_premium(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": True, "pct_of_premium": 10.0}},
        ):
            rs, pct = profit_milestone_rs(investment_rs=8000.0)
            assert rs == pytest.approx(800.0)
            assert pct == 10.0

    def test_line_none_until_peak_covers_giveback(self):
        assert profit_milestone_line_rs(peak_rs=300.0, giveback_rs=400.0) is None
        assert profit_milestone_line_rs(peak_rs=400.0, giveback_rs=400.0) == pytest.approx(0.0)
        assert profit_milestone_line_rs(peak_rs=2000.0, giveback_rs=400.0) == pytest.approx(1600.0)

    def test_line_none_when_giveback_zero(self):
        assert profit_milestone_line_rs(peak_rs=2000.0, giveback_rs=0.0) is None

    def test_auto_close_defaults_true(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert profit_milestone_config()["auto_close"] is True

    def test_confirm_seconds_defaults_to_20(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert profit_milestone_config()["confirm_seconds"] == 20

    def test_independent_of_loss_milestone_pct(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {
                "loss_milestone_alert": {"enabled": True, "pct_of_premium": 5.0},
                "profit_milestone_alert": {"enabled": True, "pct_of_premium": 12.0},
            },
        ):
            assert loss_milestone_config()["pct_of_premium"] == 5.0
            assert profit_milestone_config()["pct_of_premium"] == 12.0
