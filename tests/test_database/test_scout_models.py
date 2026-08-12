"""Direct tests for database/scout_models repositories."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from database.scout_models import (
    ScoutConfigRepo,
    ScoutScanLogRepo,
    ScoutSignalRepo,
    ScoutTradeRepo,
)


def test_scout_signal_repo_recent_parses_meta_json():
    db = MagicMock()
    db.fetch_all.return_value = [{
        "id": 1,
        "scan_id": "s1",
        "symbol": "TCS",
        "exchange": "NSE",
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "reason": "r",
        "ltp": 4000.0,
        "invalidation": 3950.0,
        "strength": "MEDIUM",
        "triggered_at": datetime(2026, 8, 12, 10, 0, 0),
        "meta_json": json.dumps({"or_high": 4010}),
    }]
    rows = ScoutSignalRepo(db).recent(limit=5)
    assert rows[0]["meta"]["or_high"] == 4010


def test_scout_signal_repo_corrupt_meta_json_returns_none_meta():
    db = MagicMock()
    db.fetch_all.return_value = [{
        "id": 1,
        "scan_id": "s1",
        "symbol": "TCS",
        "exchange": "NSE",
        "action": "BUY",
        "signal_type": "OR_BREAK_UP",
        "reason": "r",
        "ltp": 4000.0,
        "invalidation": 3950.0,
        "strength": "MEDIUM",
        "triggered_at": datetime(2026, 8, 12, 10, 0, 0),
        "meta_json": "{bad",
    }]
    rows = ScoutSignalRepo(db).recent(limit=5)
    assert rows[0]["meta"] is None


def test_scout_trade_repo_close_already_closed_returns_none():
    db = MagicMock()
    db.fetch_one.return_value = {"id": 5, "status": "CLOSED", "action": "BUY", "entry_price": 100, "quantity": 1}
    assert ScoutTradeRepo(db).close(5, exit_price=105.0, closed_at=datetime(2026, 8, 12, 15, 0, 0)) is None


def test_scout_trade_repo_void_open_trade():
    db = MagicMock()
    cur = MagicMock()
    cur.rowcount = 1
    db.execute.return_value = cur
    assert ScoutTradeRepo(db).void(7) is True


def test_scout_trade_repo_void_missing_returns_false():
    db = MagicMock()
    cur = MagicMock()
    cur.rowcount = 0
    db.execute.return_value = cur
    assert ScoutTradeRepo(db).void(99) is False


def test_scout_trade_repo_closed_trades_applies_symbol_filter():
    db = MagicMock()
    db.fetch_all.return_value = []
    ScoutTradeRepo(db).closed_trades(symbol="reliance", limit=10)
    sql = db.fetch_all.call_args.args[0]
    assert "t.symbol = ?" in sql
    assert db.fetch_all.call_args.args[1][-1] == "RELIANCE"


def test_scout_trade_repo_performance_stats_automation_split():
    db = MagicMock()
    db.fetch_all.return_value = [
        {
            "pnl": 100.0,
            "pnl_pct": 2.0,
            "action": "BUY",
            "signal_type": "OR_BREAK_UP",
            "symbol": "TCS",
            "notes": json.dumps({"mode": "auto", "source": "auto_execute"}),
            "exit_reason": "target_hit",
        },
        {
            "pnl": -50.0,
            "pnl_pct": -1.0,
            "action": "BUY",
            "signal_type": "OR_BREAK_UP",
            "symbol": "INFY",
            "notes": "manual entry",
            "exit_reason": "manual",
        },
    ]
    stats = ScoutTradeRepo(db).performance_stats()
    assert stats["total_trades"] == 2
    assert stats["wins"] == 1
    assert stats["automation"]["auto_entry_count"] == 1
    assert stats["automation"]["manual_entry_count"] == 1


def test_scout_config_repo_corrupt_json_returns_none():
    db = MagicMock()
    db.fetch_one.return_value = {"config_value": "not-json"}
    assert ScoutConfigRepo(db).get_json("settings") is None


def test_scout_config_repo_watchlist_non_list_returns_none():
    db = MagicMock()
    db.fetch_one.return_value = {"config_value": json.dumps({"bad": True})}
    assert ScoutConfigRepo(db).get_watchlist() is None


def test_scout_scan_log_repo_last_success():
    db = MagicMock()
    db.fetch_one.return_value = {
        "scan_id": "scan-1",
        "started_at": datetime(2026, 8, 12, 9, 0, 0),
        "finished_at": datetime(2026, 8, 12, 9, 5, 0),
        "symbols_scanned": 10,
        "signals_found": 2,
    }
    row = ScoutScanLogRepo(db).last_success()
    assert row["scan_id"] == "scan-1"
    assert row["signals_found"] == 2


def test_scout_scan_log_repo_start_and_finish():
    db = MagicMock()
    repo = ScoutScanLogRepo(db)
    repo.start("scan-x", datetime(2026, 8, 12, 9, 0, 0))
    repo.finish(
        "scan-x",
        status="SUCCESS",
        finished_at=datetime(2026, 8, 12, 9, 1, 0),
        symbols_scanned=5,
        signals_found=1,
    )
    assert db.execute.call_count == 2
