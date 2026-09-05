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


def test_orphan_entry_fills_query():
    db = MagicMock()
    db.fetch_all.return_value = [{"leg_order": 1, "status": "COMPLETE"}]
    rows = BrokerOrderRepo(db).orphan_entry_fills("SUG-1")
    assert rows[0]["leg_order"] == 1
    sql, params = db.fetch_all.call_args[0]
    assert "trade_id IS NULL" in sql
    assert "operation = 'ENTRY'" in sql
    assert params == ["SUG-1"]


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
