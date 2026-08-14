"""Tests for scout signal dedupe helpers and DB gate."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from database.scout_models import ScoutSignalRepo
from scout.signal_dedupe import (
    build_dedupe_cache,
    is_within_dedupe_window,
    signal_dedupe_key,
)


def test_signal_dedupe_key_per_symbol():
    assert signal_dedupe_key("reliance", "OR_BREAK_UP", dedupe_per_symbol=True) == "RELIANCE"
    assert signal_dedupe_key("reliance", "OR_BREAK_UP", dedupe_per_symbol=False) == "RELIANCE:OR_BREAK_UP"


def test_is_within_dedupe_window():
    at = datetime(2026, 8, 14, 10, 0, 0)
    prev = at - timedelta(minutes=10)
    assert is_within_dedupe_window(prev, at, 30) is True
    assert is_within_dedupe_window(prev, at, 5) is False
    assert is_within_dedupe_window(None, at, 30) is False


def test_build_dedupe_cache_per_symbol_takes_latest():
    t1 = datetime(2026, 8, 14, 10, 0, 0)
    t2 = datetime(2026, 8, 14, 10, 5, 0)
    cache = build_dedupe_cache(
        [
            {"symbol": "RELIANCE", "signal_type": "OR_BREAK_UP", "triggered_at": t1},
            {"symbol": "RELIANCE", "signal_type": "PULLBACK_DOWN", "triggered_at": t2},
        ],
        dedupe_per_symbol=True,
    )
    assert cache["RELIANCE"] == t2


def test_has_recent_duplicate_per_symbol():
    db = MagicMock()
    db.fetch_one.return_value = {"ok": 1}
    repo = ScoutSignalRepo(db)
    since = datetime(2026, 8, 14, 9, 0, 0)
    assert repo.has_recent_duplicate(
        symbol="RELIANCE",
        signal_type="OR_BREAK_UP",
        since_at=since,
        dedupe_per_symbol=True,
    )
    sql = db.fetch_one.call_args[0][0]
    assert "signal_type" not in sql


def test_has_recent_duplicate_per_type():
    db = MagicMock()
    db.fetch_one.return_value = None
    repo = ScoutSignalRepo(db)
    since = datetime(2026, 8, 14, 9, 0, 0)
    assert not repo.has_recent_duplicate(
        symbol="RELIANCE",
        signal_type="OR_BREAK_UP",
        since_at=since,
        dedupe_per_symbol=False,
    )
    sql = db.fetch_one.call_args[0][0]
    assert "signal_type" in sql
