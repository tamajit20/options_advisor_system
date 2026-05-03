"""Unit tests for engine.iv_calculator — Black-Scholes IV via bisection."""
from __future__ import annotations

import math

import pytest

from engine.iv_calculator import _bs_price, black_scholes_delta, implied_vol


class TestBsPrice:
    def test_zero_time_to_expiry_returns_intrinsic_call(self):
        assert _bs_price(23000, 22500, 0, 0.065, 0.18, "CE") == 500.0

    def test_zero_time_to_expiry_returns_intrinsic_put(self):
        assert _bs_price(22500, 23000, 0, 0.065, 0.18, "PE") == 500.0

    def test_otm_call_at_expiry_is_zero(self):
        assert _bs_price(23000, 23500, 0, 0.065, 0.18, "CE") == 0.0


class TestImpliedVol:
    """Round-trip: BS price → IV → re-price ≈ original."""

    @pytest.mark.parametrize("spot,strike,opt_type,vol_target", [
        (23000, 23000, "CE", 0.18),  # ATM call
        (23000, 23000, "PE", 0.20),  # ATM put
        (23000, 22500, "CE", 0.22),  # ITM call
        (23000, 23500, "CE", 0.16),  # OTM call
        (23000, 23500, "PE", 0.20),  # ITM put
        (23000, 22500, "PE", 0.18),  # OTM put
    ])
    def test_round_trip_recovers_vol_within_tolerance(self, spot, strike, opt_type, vol_target):
        dte = 14
        t = dte / 365.0
        market_price = _bs_price(spot, strike, t, 0.065, vol_target, opt_type)
        iv, converged = implied_vol(market_price, spot, strike, dte, opt_type)
        assert converged is True
        assert iv == pytest.approx(vol_target, abs=0.005)

    def test_negative_market_price_returns_zero(self):
        iv, ok = implied_vol(-1.0, 23000, 23000, 14, "CE")
        assert iv == 0.0 and ok is False

    def test_zero_dte_returns_zero(self):
        iv, ok = implied_vol(100.0, 23000, 23000, 0, "CE")
        assert iv == 0.0 and ok is False

    def test_arbitrage_violation_returns_zero(self):
        # Call price below intrinsic = arbitrage
        iv, ok = implied_vol(50.0, 23000, 22500, 14, "CE")  # intrinsic ≈ 500
        assert ok is False


class TestBlackScholesDelta:
    def test_atm_call_delta_around_half(self):
        d = black_scholes_delta(23000, 23000, 14, 0.18, "CE")
        assert 0.45 < d < 0.60

    def test_atm_put_delta_around_minus_half(self):
        d = black_scholes_delta(23000, 23000, 14, 0.18, "PE")
        assert -0.55 < d < -0.40

    def test_deep_itm_call_delta_near_one(self):
        d = black_scholes_delta(23000, 21000, 14, 0.18, "CE")
        assert d > 0.95

    def test_deep_otm_call_delta_near_zero(self):
        d = black_scholes_delta(23000, 25000, 14, 0.18, "CE")
        assert d < 0.10

    def test_zero_dte_returns_zero(self):
        assert black_scholes_delta(23000, 23000, 0, 0.18, "CE") == 0.0
