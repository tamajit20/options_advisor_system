"""Tests for TradeMtmSnapshotRepo."""
from datetime import datetime
from unittest.mock import MagicMock

from database.models import TradeMtmSnapshotRepo


def test_hour_bucket_truncates_to_hour():
    dt = datetime(2026, 5, 22, 11, 47, 33)
    assert TradeMtmSnapshotRepo.hour_bucket(dt) == datetime(2026, 5, 22, 11, 0, 0)


def test_upsert_hourly_deletes_then_inserts():
    db = MagicMock()
    repo = TradeMtmSnapshotRepo(db)
    repo.upsert_hourly({
        "trade_id": "TRD-20260506-001",
        "trade_name": "BNIFTY-STRADDLE",
        "mtm": -35000.0,
        "dte": 4,
        "max_profit": 0.0,
        "max_loss": 70393.75,
        "as_of": "2026-05-22T11:47:00",
        "leg_ltps": {"BANKNIFTY|54900.0|CE": 146.0},
        "feed_source": "live",
    })
    assert db.execute.call_count == 2
    delete_sql = db.execute.call_args_list[0][0][0]
    insert_sql = db.execute.call_args_list[1][0][0]
    assert "DELETE FROM options_trade_mtm_snapshot" in delete_sql
    assert "INSERT INTO options_trade_mtm_snapshot" in insert_sql


def test_archive_trade_moves_rows():
    db = MagicMock()
    db.fetch_all.return_value = [{
        "trade_id": "TRD-20260506-001",
        "trade_name": "X",
        "snapshot_at": datetime(2026, 5, 22, 11, 0, 0),
        "snapshot_granularity": "hourly",
        "mtm": -1000,
        "max_profit": 0,
        "max_loss": 5000,
        "dte": 4,
        "leg_ltps_json": "{}",
        "feed_source": "live",
        "created_at": datetime(2026, 5, 22, 11, 0, 0),
    }]
    cur = MagicMock()
    cur.rowcount = 1
    db.execute.return_value = cur
    repo = TradeMtmSnapshotRepo(db)
    n = repo.archive_trade("TRD-20260506-001")
    assert n == 1
    insert_sql = db.execute.call_args_list[0][0][0]
    delete_sql = db.execute.call_args_list[1][0][0]
    assert "INSERT INTO options_trade_mtm_snapshot_history" in insert_sql
    assert "DELETE FROM options_trade_mtm_snapshot" in delete_sql
