"""Tests for engine.greeks_exit — Greek stress checks."""
from __future__ import annotations

from engine.greeks_exit import greeks_stress_check


def test_vega_stress_on_credit_trade_near_expiry():
    reason = greeks_stress_check(
        strategy="IRON_CONDOR",
        days_to_expiry=5,
        current_pnl=-2000.0,
        max_loss_rs=15000.0,
        greeks={"net_vega": 2500.0, "net_delta": 100.0},
    )
    assert reason is not None
    assert "vega" in reason.lower()


def test_no_stress_without_greeks():
    assert greeks_stress_check(
        strategy="IRON_CONDOR",
        days_to_expiry=5,
        current_pnl=-2000.0,
        max_loss_rs=15000.0,
        greeks=None,
    ) is None
