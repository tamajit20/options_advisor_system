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
    assert groups[0]["headline"] == "You placed entry, then closed"
    assert groups[0]["badge"] == "COMPLETE"


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
    assert groups[0]["headline"] == "You placed entry — failed"
    assert groups[0]["badge"] == "FAILED"
    assert groups[0]["detail"] == "timeout"


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
    assert groups[0]["headline"] == "You placed entry — partial"
    assert groups[0]["badge"] == "PARTIAL"


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


def test_live_close_progress_uses_latest_exit_job_only():
    from lifecycle.zerodha_execution_log import live_suggestion_order_status

    rows = [
        {
            "id": 1, "execution_job_id": 8, "operation": "EXIT",
            "leg_order": 1, "status": "COMPLETE", "trade_id": "TRD-1",
        },
        {
            "id": 2, "execution_job_id": 9, "operation": "EXIT",
            "leg_order": 1, "status": "OPEN", "trade_id": "TRD-1",
        },
    ]
    progress = live_suggestion_order_status(
        rows, {"id": 9, "status": "RUNNING", "total_legs": 1},
        operation="EXIT",
    )
    assert progress["overall_status"] == "IN_FLIGHT"
    assert progress["filled_count"] == 0
    assert progress["total_orders"] == 1
    assert all(r.get("execution_job_id") == 9 for r in progress["orders"])


def test_live_close_progress_without_job_does_not_count_old_exits():
    from lifecycle.zerodha_execution_log import live_suggestion_order_status

    rows = [
        {
            "id": 1, "execution_job_id": None, "operation": "EXIT",
            "leg_order": 1, "status": "COMPLETE",
        },
    ]
    progress = live_suggestion_order_status(rows, None, operation="EXIT")
    assert progress["overall_status"] == "NONE"
    assert progress["filled_count"] == 0
    assert progress["orders"] == []


def test_failed_ip_uses_job_error_as_reason():
    rows = [
        {
            "id": 1, "trade_id": None, "suggestion_id": "SUG-Z",
            "execution_job_id": 2, "operation": "ENTRY", "leg_order": 1,
            "status": "FAILED", "created_at": datetime(2026, 9, 7, 11, 49, 15),
        },
    ]
    jobs = [{
        "id": 2,
        "error_message": (
            "No IPs configured for this API. Please add your IP. "
            "Learn more: https://kite.trade/docs"
        ),
    }]
    groups = group_broker_orders(rows, jobs=jobs)
    assert groups[0]["headline"] == "You placed entry — failed"
    assert "no static IPs" in groups[0]["detail"]
    assert "https://" not in groups[0]["detail"]


def test_merges_unlinked_rollback_into_entry_and_explains_revert():
    rows = [
        {
            "id": 1, "trade_id": None, "suggestion_id": "SUG-Z",
            "execution_job_id": 3, "operation": "ENTRY", "leg_order": 1,
            "status": "COMPLETE", "transaction_type": "BUY",
            "created_at": datetime(2026, 9, 7, 11, 57, 19),
            "updated_at": datetime(2026, 9, 7, 11, 57, 20),
        },
        {
            "id": 2, "trade_id": None, "suggestion_id": "SUG-Z",
            "execution_job_id": None, "operation": "ROLLBACK", "leg_order": 1,
            "status": "COMPLETE", "transaction_type": "SELL",
            "created_at": datetime(2026, 9, 7, 11, 57, 20),
            "updated_at": datetime(2026, 9, 7, 11, 57, 21),
        },
    ]
    jobs = [{
        "id": 3,
        "error_message": (
            "Prior Zerodha entry fills exist without a recorded trade; "
            "flatten on kite first."
        ),
    }]
    groups = group_broker_orders(rows, jobs=jobs)
    assert len(groups) == 1
    assert groups[0]["group_key"] == "job:3"
    assert groups[0]["operations"] == ["ENTRY", "ROLLBACK"]
    assert groups[0]["headline"] == "You placed entry · system reverted"
    assert groups[0]["badge"] == "REVERTED"
    assert "leftovers" in groups[0]["detail"]


def test_recorded_entry_headline():
    rows = [
        {
            "id": 1, "trade_id": "TRD-9", "suggestion_id": "S1",
            "execution_job_id": 8, "operation": "ENTRY", "leg_order": 1,
            "status": "COMPLETE", "created_at": datetime(2026, 9, 7, 12, 0),
        },
    ]
    groups = group_broker_orders(rows)
    assert groups[0]["headline"] == "You placed entry"
    assert groups[0]["detail"] == "Trade TRD-9 recorded."
    assert groups[0]["badge"] == "COMPLETE"
