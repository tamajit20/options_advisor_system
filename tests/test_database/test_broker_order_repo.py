"""Tests for database/broker_order_repo.py retention."""

from datetime import date, datetime
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
