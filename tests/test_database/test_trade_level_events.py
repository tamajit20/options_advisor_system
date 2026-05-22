"""Tests for options_trade_level_events persistence."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from database.models import TradeLevelEventRepo


def test_insert_executes_sql():
    db = MagicMock()
    repo = TradeLevelEventRepo(db)
    repo.insert({
        "trade_id": "T-001",
        "level_type": "LOSS_LIMIT",
        "event_type": "ENTER",
        "event_at": datetime(2026, 5, 22, 10, 30),
        "mtm": -4000.0,
        "threshold_rs": 3685.5,
        "spot": 24500.0,
        "leg_ltps": {"NIFTY|26000|PE": 120.5},
    })
    sql = db.execute.call_args[0][0]
    args = db.execute.call_args[0][1]
    assert "INSERT INTO options_trade_level_events" in sql
    assert args[0] == "T-001"
    assert args[1] == "LOSS_LIMIT"
    assert args[2] == "ENTER"
    assert "NIFTY|26000|PE" in args[7]


def test_list_for_trade_queries():
    db = MagicMock()
    db.fetch_all.return_value = [{"event_type": "EXIT"}]
    rows = TradeLevelEventRepo(db).list_for_trade("T-001", limit=10)
    assert rows[0]["event_type"] == "EXIT"
    assert "options_trade_level_events" in db.fetch_all.call_args[0][0]


def test_invalid_level_raises():
    repo = TradeLevelEventRepo(MagicMock())
    with pytest.raises(ValueError):
        repo.insert({
            "trade_id": "T-001",
            "level_type": "BAD",
            "event_type": "ENTER",
            "mtm": 0,
        })
