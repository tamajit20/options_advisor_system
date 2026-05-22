"""Tests for engine/stop_loss_levels.py — every strategy's spot SL bands."""

from __future__ import annotations

from datetime import date

import pytest

from contracts import SuggestionLeg
from engine.stop_loss_levels import (
    compute_spot_stop_bands,
    primary_stop_loss_level,
    spot_stop_breached,
)


def _leg(order, action, strike, opt, price=100.0):
    return SuggestionLeg(
        leg_order=order, hedge_pair_leg=None, symbol="NIFTY",
        expiry_date=date(2026, 6, 1), strike=float(strike), option_type=opt,
        action=action, lots=1, lot_size=75,
        suggested_price=price, suggested_price_low=price * 0.95,
        suggested_price_high=price * 1.05, leg_purpose_note="",
    )


class TestDebitAndLongNoSpotSl:
    @pytest.mark.parametrize("strategy", [
        "BEAR_PUT_SPREAD", "BULL_CALL_SPREAD",
        "LONG_STRADDLE", "LONG_STRANGLE", "LONG_CALL", "LONG_PUT",
    ])
    def test_no_bands(self, strategy):
        legs = [_leg(1, "BUY", 23500, "CE"), _leg(2, "SELL", 23200, "CE")]
        assert compute_spot_stop_bands(legs, strategy) == (None, None)
        assert primary_stop_loss_level(legs, strategy) is None
        assert not spot_stop_breached(strategy=strategy, spot=24000, legs=legs)


class TestBearPutSpreadRegression:
    """TRD-20260520-001: old formula gave 23475 and wrong 'stay above' UI."""

    def test_no_spot_bands(self):
        legs = [_leg(1, "BUY", 23650, "PE"), _leg(2, "SELL", 23300, "PE")]
        assert compute_spot_stop_bands(legs, "BEAR_PUT_SPREAD") == (None, None)


class TestBullPutSpread:
    def test_lower_band_only(self):
        legs = [_leg(1, "SELL", 23400, "PE"), _leg(2, "BUY", 23000, "PE")]
        upper, lower = compute_spot_stop_bands(legs, "BULL_PUT_SPREAD")
        assert upper is None
        assert lower == pytest.approx(23200.0)  # 23400 - 200
        assert primary_stop_loss_level(legs, "BULL_PUT_SPREAD") == lower
        assert spot_stop_breached(strategy="BULL_PUT_SPREAD", spot=23100, legs=legs)
        assert not spot_stop_breached(strategy="BULL_PUT_SPREAD", spot=23300, legs=legs)


class TestBearCallSpread:
    def test_upper_band_only(self):
        legs = [_leg(1, "SELL", 23800, "CE"), _leg(2, "BUY", 24100, "CE")]
        upper, lower = compute_spot_stop_bands(legs, "BEAR_CALL_SPREAD")
        assert upper == pytest.approx(23950.0)  # 23800 + 150
        assert lower is None
        assert spot_stop_breached(strategy="BEAR_CALL_SPREAD", spot=24000, legs=legs)
        assert not spot_stop_breached(strategy="BEAR_CALL_SPREAD", spot=23800, legs=legs)


class TestIronCondor:
    def test_both_bands_symmetric_wing(self):
        legs = [
            _leg(1, "SELL", 23200, "PE"), _leg(2, "BUY", 22900, "PE"),
            _leg(3, "SELL", 24100, "CE"), _leg(4, "BUY", 24400, "CE"),
        ]
        upper, lower = compute_spot_stop_bands(legs, "IRON_CONDOR")
        assert upper == pytest.approx(24250.0)  # 24100 + 150
        assert lower == pytest.approx(23050.0)  # 23200 - 150
        assert primary_stop_loss_level(legs, "IRON_CONDOR") == upper
        assert spot_stop_breached(strategy="IRON_CONDOR", spot=24300, legs=legs)
        assert spot_stop_breached(strategy="IRON_CONDOR", spot=23000, legs=legs)
        assert not spot_stop_breached(strategy="IRON_CONDOR", spot=23600, legs=legs)


class TestIronButterfly:
    def test_both_bands_at_atm(self):
        atm = 23500.0
        legs = [
            _leg(1, "SELL", atm, "PE"), _leg(2, "BUY", 23200, "PE"),
            _leg(3, "SELL", atm, "CE"), _leg(4, "BUY", 23800, "CE"),
        ]
        upper, lower = compute_spot_stop_bands(legs, "IRON_BUTTERFLY")
        assert upper == pytest.approx(23650.0)
        assert lower == pytest.approx(23350.0)


class TestJadeLizard:
    def test_lower_only_no_false_rally_sl(self):
        legs = [
            _leg(1, "SELL", 23000, "PE"),
            _leg(2, "SELL", 23800, "CE"), _leg(3, "BUY", 24100, "CE"),
        ]
        upper, lower = compute_spot_stop_bands(legs, "JADE_LIZARD", net_premium_per_share=50.0)
        assert upper is None
        assert lower == pytest.approx(22975.0)  # 23000 - 50*0.5
        assert not spot_stop_breached(strategy="JADE_LIZARD", spot=24000, legs=legs, net_premium_per_share=50.0)
        assert spot_stop_breached(strategy="JADE_LIZARD", spot=22900, legs=legs, net_premium_per_share=50.0)
