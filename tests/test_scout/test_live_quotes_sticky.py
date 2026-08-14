"""Sticky live quote cache — symbols must not vanish between tick intervals."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from scout import live_quotes


def _write_snap(tmp_path, payload):
    path = tmp_path / "ws_status.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_last_equity_map_used_when_missing_from_recent_events(tmp_path, monkeypatch):
    fresh_ts = datetime.now().isoformat()
    snap = {
        "generated_at": fresh_ts,
        "last_equity_ltps": {
            "RELIANCE": {"ltp": 2500.25, "as_of": fresh_ts},
        },
        "recent_events": [
            {"topic": "tick.scout", "symbol": "TCS", "last_price": 4000.0, "ts": fresh_ts},
        ],
    }
    path = _write_snap(tmp_path, snap)
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    live_quotes._STICKY_QUOTES.clear()

    quotes = live_quotes.latest_equity_ltps(["RELIANCE"], max_age_seconds=45)
    assert quotes["RELIANCE"]["ltp"] == 2500.25


def test_sticky_cache_keeps_symbol_between_polls(tmp_path, monkeypatch):
    fresh_ts = datetime.now().isoformat()
    snap1 = {
        "generated_at": fresh_ts,
        "recent_events": [
            {"topic": "tick.scout", "symbol": "INFY", "last_price": 1800.0, "ts": fresh_ts},
        ],
    }
    path = _write_snap(tmp_path, snap1)
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    live_quotes._STICKY_QUOTES.clear()

    first = live_quotes.latest_equity_ltps(["INFY"], max_age_seconds=45)
    assert first["INFY"]["ltp"] == 1800.0

    snap2 = {
        "generated_at": fresh_ts,
        "recent_events": [
            {"topic": "tick.scout", "symbol": "TCS", "last_price": 4000.0, "ts": fresh_ts},
        ],
    }
    path.write_text(json.dumps(snap2), encoding="utf-8")

    second = live_quotes.latest_equity_ltps(["INFY"], max_age_seconds=45)
    assert second["INFY"]["ltp"] == 1800.0
    assert second["INFY"]["stale"] is True


def test_stale_snapshot_still_returns_last_map(tmp_path, monkeypatch):
    old_gen = (datetime.now() - timedelta(seconds=120)).isoformat()
    tick_ts = (datetime.now() - timedelta(seconds=10)).isoformat()
    snap = {
        "generated_at": old_gen,
        "last_equity_ltps": {
            "HDFCBANK": {"ltp": 1650.0, "as_of": tick_ts},
        },
        "recent_events": [],
    }
    path = _write_snap(tmp_path, snap)
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    live_quotes._STICKY_QUOTES.clear()

    quotes = live_quotes.latest_equity_ltps(["HDFCBANK"], max_age_seconds=45)
    assert quotes["HDFCBANK"]["ltp"] == 1650.0
    assert quotes["HDFCBANK"]["stale"] is True
