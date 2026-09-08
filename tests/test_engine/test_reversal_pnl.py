"""Round-trip P&L for auto-reverted Zerodha fills."""
from __future__ import annotations

from engine.charges import estimate_charges_per_txn
from engine.reversal_pnl import compute_reversal_from_orders, order_fingerprint


def _row(oid, op, txn, qty, px, leg=1, status="COMPLETE"):
    return {
        "id": oid,
        "operation": op,
        "transaction_type": txn,
        "filled_quantity": qty,
        "fill_price": px,
        "leg_order": leg,
        "status": status,
        "tradingsymbol": "NIFTY26MAY23000CE",
    }


class TestComputeReversal:
    def test_long_round_trip_loss_and_charges(self):
        rows = [
            _row(1, "ENTRY", "BUY", 50, 100.0),
            _row(2, "ROLLBACK", "SELL", 50, 98.0),
        ]
        snap = compute_reversal_from_orders(rows, open_operation="ENTRY")
        assert snap is not None
        assert snap["matched_qty"] == 50
        assert snap["gross_pnl"] == -100.0
        expected = estimate_charges_per_txn([
            {"action": "BUY", "price": 100.0, "lots": 1, "lot_size": 50},
            {"action": "SELL", "price": 98.0, "lots": 1, "lot_size": 50},
        ])
        assert snap["total_charges"] == expected.total
        assert snap["net_pnl"] == round(-100.0 - expected.total, 2)
        assert snap["brokerage"] == expected.brokerage

    def test_short_round_trip_loss(self):
        rows = [
            _row(1, "ENTRY", "SELL", 75, 40.0),
            _row(2, "ROLLBACK", "BUY", 75, 41.0),
        ]
        snap = compute_reversal_from_orders(rows)
        assert snap["gross_pnl"] == -75.0
        assert snap["matched_qty"] == 75

    def test_partial_fill_matches_qty(self):
        rows = [
            _row(1, "ENTRY", "BUY", 25, 10.0, status="PARTIAL"),
            _row(2, "ROLLBACK", "SELL", 25, 10.2),
        ]
        snap = compute_reversal_from_orders(rows)
        assert snap["matched_qty"] == 25
        assert snap["gross_pnl"] == 5.0  # (10.2-10)*25

    def test_skips_when_nothing_was_flattened(self):
        rows = [_row(1, "ENTRY", "BUY", 50, 100.0)]
        assert compute_reversal_from_orders(rows) is None

    def test_fingerprint_stable(self):
        rows = [
            _row(1, "ENTRY", "BUY", 50, 100.0),
            _row(2, "ROLLBACK", "SELL", 50, 99.0),
        ]
        a = order_fingerprint(rows)
        b = order_fingerprint(list(reversed(rows)))
        assert a == b
        assert len(a) == 40
