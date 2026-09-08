"""Tests for execution reversal ledger recording."""
from __future__ import annotations

from unittest.mock import MagicMock

from lifecycle.execution_reversal import record_reversal_from_orders


def test_inserts_once_per_fingerprint(mocker):
    db = MagicMock()
    repo = MagicMock()
    repo.get_by_fingerprint.return_value = None
    repo.get_by_job.return_value = None
    repo.insert.return_value = 9
    mocker.patch(
        "lifecycle.execution_reversal.ExecutionReversalRepo",
        return_value=repo,
    )
    mocker.patch("lifecycle.execution_reversal.NotificationRepo")
    orders = [
        {
            "id": 1, "operation": "ENTRY", "transaction_type": "BUY",
            "filled_quantity": 50, "fill_price": 100.0, "leg_order": 1,
            "status": "COMPLETE", "tradingsymbol": "X",
        },
        {
            "id": 2, "operation": "ROLLBACK", "transaction_type": "SELL",
            "filled_quantity": 50, "fill_price": 99.0, "leg_order": 1,
            "status": "COMPLETE", "tradingsymbol": "X",
        },
    ]
    row = record_reversal_from_orders(
        db, orders, suggestion_id="S1", execution_job_id=7,
        reason="ENTRY_ROLLBACK",
    )
    assert row["id"] == 9
    assert repo.insert.call_count == 1
    db.commit.assert_called()

    repo.get_by_fingerprint.return_value = {"id": 9, "net_pnl": -1}
    again = record_reversal_from_orders(
        db, orders, suggestion_id="S1", execution_job_id=7,
        reason="ENTRY_ROLLBACK",
    )
    assert again["id"] == 9
    assert repo.insert.call_count == 1
