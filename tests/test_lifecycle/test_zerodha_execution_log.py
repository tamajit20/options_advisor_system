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


def test_jobs_on_same_suggestion_are_separate_groups():
    rows = [
        {
            "id": 1, "trade_id": None, "suggestion_id": "S9",
            "execution_job_id": 2, "operation": "ENTRY", "leg_order": 1,
            "status": "FAILED", "created_at": datetime(2026, 9, 7, 11, 49),
        },
        {
            "id": 2, "trade_id": None, "suggestion_id": "S9",
            "execution_job_id": 3, "operation": "ENTRY", "leg_order": 1,
            "status": "COMPLETE", "created_at": datetime(2026, 9, 7, 11, 57),
        },
    ]
    groups = group_broker_orders(rows)
    assert len(groups) == 2
    by_key = {g["group_key"]: g for g in groups}
    assert by_key["job:2"]["overall_status"] == "FAILED"
    assert by_key["job:3"]["overall_status"] == "COMPLETE"


def test_live_progress_ignores_prior_failed_attempt():
    from lifecycle.zerodha_execution_log import live_suggestion_order_status

    rows = [
        {
            "id": 1, "execution_job_id": 2, "operation": "ENTRY",
            "leg_order": 1, "status": "FAILED", "trade_id": None,
        },
        {
            "id": 2, "execution_job_id": 3, "operation": "ENTRY",
            "leg_order": 1, "status": "COMPLETE", "trade_id": None,
        },
        {
            "id": 3, "execution_job_id": 3, "operation": "ENTRY",
            "leg_order": 2, "status": "COMPLETE", "trade_id": None,
        },
        {
            "id": 4, "execution_job_id": 3, "operation": "ROLLBACK",
            "leg_order": 2, "status": "COMPLETE",
        },
        {
            "id": 5, "execution_job_id": 3, "operation": "ROLLBACK",
            "leg_order": 1, "status": "COMPLETE",
        },
    ]
    progress = live_suggestion_order_status(
        rows, {"id": 3, "status": "FAILED", "total_legs": 2},
    )
    assert progress["overall_status"] == "FAILED"
    assert progress["filled_count"] == 2
    assert progress["total_orders"] == 2
    assert all(r.get("execution_job_id") == 3 for r in progress["orders"])
