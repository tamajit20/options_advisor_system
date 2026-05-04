"""
tests/test_engine/test_adverse_move_advisor.py
==============================================

Pure-function tests for engine/adverse_move_advisor.py.
"""

from __future__ import annotations

import pytest

from engine.adverse_move_advisor import assess_adverse_move


class TestNoOpCases:
    def test_winning_trade_returns_none(self):
        assert assess_adverse_move(current_pnl=500.0, max_loss_rs=10000.0) is None

    def test_flat_trade_returns_none(self):
        assert assess_adverse_move(current_pnl=0.0, max_loss_rs=10000.0) is None

    def test_max_loss_zero_returns_none(self):
        assert assess_adverse_move(current_pnl=-500.0, max_loss_rs=0.0) is None

    def test_below_warning_threshold_returns_none(self):
        # 25% of 10k = 2500 < 30% threshold
        assert assess_adverse_move(
            current_pnl=-2500.0, max_loss_rs=10000.0,
            warning_pct=30.0, sl_pct=60.0,
        ) is None

    def test_at_or_above_sl_returns_none(self):
        # SL territory — caller fires SL_HIT instead
        result = assess_adverse_move(
            current_pnl=-7000.0, max_loss_rs=10000.0,
            warning_pct=30.0, sl_pct=60.0,
        )
        assert result is None


class TestWarningBand:
    def test_at_threshold_fires(self):
        result = assess_adverse_move(
            current_pnl=-3000.0, max_loss_rs=10000.0,
            warning_pct=30.0, sl_pct=60.0,
        )
        assert result is not None
        assert result.severity == "MODERATE"
        assert result.pnl_pct_of_max_loss == 30.0
        assert "30%" in result.headline

    def test_mid_band_fires(self):
        result = assess_adverse_move(
            current_pnl=-4500.0, max_loss_rs=10000.0,
            warning_pct=30.0, sl_pct=60.0,
        )
        assert result is not None
        assert result.pnl_pct_of_max_loss == 45.0
        assert "Adverse-move advisory" in result.recovery_hint

    def test_just_below_sl_still_fires(self):
        result = assess_adverse_move(
            current_pnl=-5900.0, max_loss_rs=10000.0,
            warning_pct=30.0, sl_pct=60.0,
        )
        assert result is not None


class TestConfigDefaults:
    def test_uses_strategy_config_when_pct_none(self, mocker):
        # Patch STRATEGY_CONFIG used by the module
        from engine import adverse_move_advisor as ama
        mocker.patch.dict(ama.STRATEGY_CONFIG, {
            "adverse_move_warning_pct": 25.0,
            "stop_loss_fraction": 0.60,
        })
        # 25% of 10k = 2500 — at threshold with config 25%
        result = assess_adverse_move(current_pnl=-2500.0, max_loss_rs=10000.0)
        assert result is not None
        assert result.pnl_pct_of_max_loss == 25.0
