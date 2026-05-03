"""Unit tests for engine.charges — Zerodha charge calculator."""
from __future__ import annotations

import pytest

from engine.charges import estimate_charges, estimate_charges_per_txn


class TestEstimateCharges:
    """`estimate_charges` doubles brokerage/exchange/sebi for an assumed exit."""

    def test_single_buy_leg(self):
        legs = [{"action": "BUY", "price": 100.0, "lots": 1, "lot_size": 75}]
        c = estimate_charges(legs)
        # 2 orders @ ₹20 = ₹40 brokerage
        assert c.brokerage == pytest.approx(40.0)
        # No STT on buy
        assert c.stt == 0.0
        # Stamp: 0.003% × 7500 = 0.225
        assert c.stamp_duty == pytest.approx(0.225, abs=0.01)
        assert c.total > 0

    def test_single_sell_leg_charges_stt(self):
        legs = [{"action": "SELL", "price": 100.0, "lots": 1, "lot_size": 75}]
        c = estimate_charges(legs)
        # STT 0.05% × 7500 = 3.75
        assert c.stt == pytest.approx(3.75, abs=0.01)
        # No stamp on sell
        assert c.stamp_duty == 0.0

    def test_zero_qty_skipped(self):
        legs = [{"action": "BUY", "price": 100.0, "lots": 0, "lot_size": 75}]
        c = estimate_charges(legs)
        assert c.total == 0.0

    def test_multi_leg_iron_condor_total_positive(self):
        legs = [
            {"action": "SELL", "price": 50.0, "lots": 1, "lot_size": 75},
            {"action": "BUY",  "price": 30.0, "lots": 1, "lot_size": 75},
            {"action": "SELL", "price": 55.0, "lots": 1, "lot_size": 75},
            {"action": "BUY",  "price": 35.0, "lots": 1, "lot_size": 75},
        ]
        c = estimate_charges(legs)
        # 4 legs × 2 orders × ₹20 = ₹160 brokerage
        assert c.brokerage == pytest.approx(160.0)
        assert c.gst > 0
        # Sanity: total = sum of components
        expected = c.brokerage + c.stt + c.exchange + c.sebi + c.stamp_duty + c.gst
        assert c.total == pytest.approx(round(expected, 2), abs=0.05)

    def test_gst_is_18_pct_of_brokerage_exchange_sebi(self):
        legs = [{"action": "SELL", "price": 100.0, "lots": 1, "lot_size": 75}]
        c = estimate_charges(legs)
        expected_gst = 0.18 * (c.brokerage + c.exchange + c.sebi)
        assert c.gst == pytest.approx(round(expected_gst, 2), abs=0.05)

    def test_itm_expiry_adds_intrinsic_stt(self):
        legs_itm = [{
            "action": "SELL", "price": 100.0, "lots": 1, "lot_size": 75,
            "is_itm_at_expiry": True, "intrinsic_at_expiry": 200.0,
        }]
        c_itm = estimate_charges(legs_itm)
        c_no_itm = estimate_charges([{
            "action": "SELL", "price": 100.0, "lots": 1, "lot_size": 75
        }])
        # ITM expiry adds 0.125% × 200 × 75 = 18.75 extra STT
        assert c_itm.stt - c_no_itm.stt == pytest.approx(18.75, abs=0.05)


class TestEstimateChargesPerTxn:
    """Single-transaction variant — each leg is one real order, no doubling."""

    def test_single_leg_no_doubling(self):
        legs = [{"action": "BUY", "price": 100.0, "lots": 1, "lot_size": 75}]
        c = estimate_charges_per_txn(legs)
        assert c.brokerage == pytest.approx(20.0)  # one order, not two

    def test_negative_price_skipped(self):
        legs = [{"action": "BUY", "price": -1.0, "lots": 1, "lot_size": 75}]
        c = estimate_charges_per_txn(legs)
        assert c.total == 0.0
