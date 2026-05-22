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
        # SL threshold 50% × 10k = 5k; warn at 30% of 5k = 1.5k
        assert assess_adverse_move(
            current_pnl=-1000.0, max_loss_rs=10000.0,
            strategy="BEAR_CALL_SPREAD",
            warning_pct=30.0, sl_pct=100.0,
        ) is None

    def test_at_or_above_sl_returns_none(self):
        result = assess_adverse_move(
            current_pnl=-7000.0, max_loss_rs=10000.0,
            strategy="BEAR_CALL_SPREAD",
            warning_pct=30.0, sl_pct=100.0,
        )
        assert result is None


class TestWarningBand:
    def test_at_threshold_fires(self):
        result = assess_adverse_move(
            current_pnl=-1500.0, max_loss_rs=10000.0,
            strategy="BEAR_CALL_SPREAD",
            warning_pct=30.0, sl_pct=100.0,
        )
        assert result is not None
        assert result.severity == "MODERATE"
        assert result.pnl_pct_of_max_loss == 30.0

    def test_mid_band_fires(self):
        result = assess_adverse_move(
            current_pnl=-2250.0, max_loss_rs=10000.0,
            strategy="BEAR_CALL_SPREAD",
            warning_pct=30.0, sl_pct=100.0,
        )
        assert result is not None
        assert result.pnl_pct_of_max_loss == 45.0
        assert "Adverse-move advisory" in result.recovery_hint

    def test_just_below_sl_still_fires(self):
        result = assess_adverse_move(
            current_pnl=-4900.0, max_loss_rs=10000.0,
            strategy="BEAR_CALL_SPREAD",
            warning_pct=30.0, sl_pct=100.0,
        )
        assert result is not None


class TestConfigDefaults:
    def test_uses_pre_breach_fraction_of_sl_threshold(self):
        # BEAR_CALL 50% SL on 10k → threshold 5k; default pre_breach 0.70 → 3.5k
        result = assess_adverse_move(
            current_pnl=-3600.0,
            max_loss_rs=10000.0,
            strategy="BEAR_CALL_SPREAD",
        )
        assert result is not None
        assert result.pnl_pct_of_max_loss == pytest.approx(72.0, abs=0.5)
