"""Tests for database/broker_order_repo.py."""

from datetime import date
from unittest.mock import MagicMock

from database.broker_order_repo import BrokerOrderRepo


def test_delete_older_than():
    db = MagicMock()
    cur = MagicMock()
    cur.rowcount = 7
    db.execute.return_value = cur
    n = BrokerOrderRepo(db).delete_older_than(date(2026, 6, 1))
    assert n == 7
    sql = db.execute.call_args[0][0]
    assert "options_broker_orders" in sql
    assert "created_at <" in sql


def test_pending_for_suggestion_only_open_statuses():
    db = MagicMock()
    db.fetch_all.return_value = [{"status": "OPEN"}]
    rows = BrokerOrderRepo(db).pending_for_suggestion("SUG-1")
    assert rows[0]["status"] == "OPEN"
    sql, params = db.fetch_all.call_args[0]
    assert "suggestion_id = ?" in sql
    assert "PENDING" in sql
    assert "OPEN" in sql
    assert "TRIGGER PENDING" in sql
    assert "COMPLETE" not in sql
    assert params == ["SUG-1"]


def test_orphan_entry_fills_uses_net_unbooked_logic():
    db = MagicMock()
    db.fetch_all.return_value = [
        {
            "id": 1, "operation": "ENTRY", "status": "FAILED",
            "kite_order_id": None, "trade_id": None, "leg_order": 1,
            "quantity": 65, "tradingsymbol": "PE", "execution_job_id": 2,
        },
    ]
    rows = BrokerOrderRepo(db).orphan_entry_fills("SUG-1")
    assert rows == []


def test_has_kite_orders_for_trade_true():
    db = MagicMock()
    db.fetch_one.return_value = {"x": 1}
    assert BrokerOrderRepo(db).has_kite_orders_for_trade("TRD-1") is True
    sql, params = db.fetch_one.call_args[0]
    assert "kite_order_id IS NOT NULL" in sql
    assert params == ["TRD-1"]


def test_has_kite_orders_for_trade_false():
    db = MagicMock()
    db.fetch_one.return_value = None
    assert BrokerOrderRepo(db).has_kite_orders_for_trade("TRD-1") is False


def test_update_status_persists_limit_price():
    db = MagicMock()
    BrokerOrderRepo(db).update_status(
        9,
        status="OPEN",
        kite_order_id="OID-1",
        limit_price=12.5,
        order_type="LIMIT",
        updated_at=None,
    )
    sql, params = db.execute.call_args[0]
    assert "limit_price = ?" in sql
    assert 12.5 in params
    assert "OID-1" in params


def test_pending_for_trade_with_operation():
    db = MagicMock()
    db.fetch_all.return_value = [{"status": "OPEN"}]
    rows = BrokerOrderRepo(db).pending_for_trade("TRD-1", operation="EXIT")
    assert len(rows) == 1
    sql, params = db.fetch_all.call_args[0]
    assert "operation = ?" in sql
    assert params == ["TRD-1", "EXIT"]


def test_failed_ip_reject_is_not_an_orphan():
    from database.broker_order_repo import net_unbooked_entry_fills

    rows = [
        {
            "id": 1, "operation": "ENTRY", "status": "FAILED",
            "kite_order_id": None, "trade_id": None, "leg_order": 1,
            "quantity": 65, "execution_job_id": 2,
        },
    ]
    assert net_unbooked_entry_fills(rows) == []


def test_rolled_back_retry_is_not_an_orphan():
    from database.broker_order_repo import net_unbooked_entry_fills

    rows = [
        {
            "id": 2, "operation": "ENTRY", "status": "COMPLETE",
            "kite_order_id": "K1", "trade_id": None, "leg_order": 1,
            "quantity": 65, "filled_quantity": 65,
            "tradingsymbol": "NIFTY2690823300PE", "execution_job_id": 3,
        },
        {
            "id": 3, "operation": "ENTRY", "status": "COMPLETE",
            "kite_order_id": "K2", "trade_id": None, "leg_order": 2,
            "quantity": 65, "filled_quantity": 65,
            "tradingsymbol": "NIFTY2690824450CE", "execution_job_id": 3,
        },
        {
            "id": 4, "operation": "ROLLBACK", "status": "COMPLETE",
            "kite_order_id": "K3", "leg_order": 2, "quantity": 65,
            "filled_quantity": 65, "tradingsymbol": "NIFTY2690824450CE",
        },
        {
            "id": 5, "operation": "ROLLBACK", "status": "COMPLETE",
            "kite_order_id": "K4", "leg_order": 1, "quantity": 65,
            "filled_quantity": 65, "tradingsymbol": "NIFTY2690823300PE",
        },
    ]
    assert net_unbooked_entry_fills(rows) == []


def test_current_job_fills_are_not_orphans_while_booking():
    from database.broker_order_repo import net_unbooked_entry_fills

    rows = [
        {
            "id": 2, "operation": "ENTRY", "status": "COMPLETE",
            "kite_order_id": "K1", "trade_id": None, "leg_order": 1,
            "quantity": 65, "filled_quantity": 65, "execution_job_id": 3,
        },
        {
            "id": 3, "operation": "ENTRY", "status": "COMPLETE",
            "kite_order_id": "K2", "trade_id": None, "leg_order": 2,
            "quantity": 65, "filled_quantity": 65, "execution_job_id": 3,
        },
    ]
    assert net_unbooked_entry_fills(rows, except_job_id=3) == []
    leftover = net_unbooked_entry_fills(rows)
    assert {r["leg_order"] for r in leftover} == {1, 2}


def test_unreversed_complete_fill_is_still_an_orphan():
    from database.broker_order_repo import net_unbooked_entry_fills

    rows = [
        {
            "id": 2, "operation": "ENTRY", "status": "COMPLETE",
            "kite_order_id": "K1", "trade_id": None, "leg_order": 1,
            "quantity": 65, "filled_quantity": 65, "execution_job_id": 3,
        },
    ]
    assert len(net_unbooked_entry_fills(rows)) == 1
