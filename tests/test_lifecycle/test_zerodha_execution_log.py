"""Tests for lifecycle/zerodha_execution_log.py"""

from datetime import datetime

from lifecycle.zerodha_execution_log import group_broker_orders


def test_groups_by_trade_id():
    rows = [
        {
            "id": 1, "trade_id": "T1", "suggestion_id": "S1",
            "operation": "ENTRY", "leg_order": 1, "status": "COMPLETE",
            "created_at": datetime(2026, 6, 1, 10, 0),
            "updated_at": datetime(2026, 6, 1, 10, 1),
        },
        {
            "id": 2, "trade_id": "T1", "suggestion_id": "S1",
            "operation": "EXIT", "leg_order": 1, "status": "COMPLETE",
            "created_at": datetime(2026, 6, 2, 15, 0),
            "updated_at": datetime(2026, 6, 2, 15, 1),
        },
    ]
    groups = group_broker_orders(rows, trade_names={"T1": "Test trade"})
    assert len(groups) == 1
    assert groups[0]["trade_id"] == "T1"
    assert groups[0]["trade_name"] == "Test trade"
    assert groups[0]["overall_status"] == "COMPLETE"
    assert len(groups[0]["orders"]) == 2
    assert groups[0]["operations"] == ["ENTRY", "EXIT"]


def test_failed_entry_groups_by_suggestion():
    rows = [
        {
            "id": 3, "trade_id": None, "suggestion_id": "S9",
            "operation": "ENTRY", "leg_order": 1, "status": "FAILED",
            "created_at": datetime(2026, 6, 3, 9, 0),
            "updated_at": datetime(2026, 6, 3, 9, 2),
            "error_message": "timeout",
        },
    ]
    groups = group_broker_orders(rows)
    assert len(groups) == 1
    assert groups[0]["suggestion_id"] == "S9"
    assert groups[0]["overall_status"] == "FAILED"


def test_partial_status():
    rows = [
        {
            "id": 4, "trade_id": "T2", "suggestion_id": "S2",
            "operation": "ENTRY", "leg_order": 1, "status": "COMPLETE",
            "created_at": datetime(2026, 6, 4, 10, 0),
            "updated_at": datetime(2026, 6, 4, 10, 1),
        },
        {
            "id": 5, "trade_id": "T2", "suggestion_id": "S2",
            "operation": "ENTRY", "leg_order": 2, "status": "FAILED",
            "created_at": datetime(2026, 6, 4, 10, 2),
            "updated_at": datetime(2026, 6, 4, 10, 5),
        },
    ]
    groups = group_broker_orders(rows)
    assert groups[0]["overall_status"] == "PARTIAL"
