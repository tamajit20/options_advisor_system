"""Extended tests for scout live_quotes (tick.scout topic)."""

from __future__ import annotations

import json

from scout.live_quotes import latest_equity_ltps


def _write_snap(tmp_path, events):
    path = tmp_path / "ws_status.json"
    path.write_text(json.dumps({"recent_events": events}), encoding="utf-8")
    return path


def test_prefers_tick_scout_topic(tmp_path, monkeypatch):
    events = [
        {"ts": "2026-08-12T10:00:00", "topic": "tick", "symbol": "RELIANCE", "last_price": 2400},
        {"ts": "2026-08-12T10:00:01", "topic": "tick.scout", "symbol": "RELIANCE", "last_price": 2500.5},
    ]
    path = _write_snap(tmp_path, events)
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    quotes = latest_equity_ltps(["RELIANCE"])
    assert quotes["RELIANCE"]["ltp"] == 2500.5


def test_legacy_tick_topic_still_works(tmp_path, monkeypatch):
    events = [
        {"ts": "2026-08-12T10:00:00", "topic": "tick", "symbol": "TCS", "last_price": 4000.0},
    ]
    path = _write_snap(tmp_path, events)
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    assert latest_equity_ltps(["TCS"])["TCS"]["ltp"] == 4000.0


def test_ignores_option_legs_on_legacy_tick(tmp_path, monkeypatch):
    events = [
        {
            "ts": "2026-08-12T10:00:00",
            "topic": "tick",
            "symbol": "RELIANCE",
            "option_type": "CE",
            "strike": 2500,
            "last_price": 50,
        },
    ]
    path = _write_snap(tmp_path, events)
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    assert latest_equity_ltps(["RELIANCE"]) == {}


def test_missing_snapshot_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: tmp_path / "missing.json")
    assert latest_equity_ltps(["RELIANCE"]) == {}


def test_corrupt_snapshot_returns_empty(tmp_path, monkeypatch):
    path = tmp_path / "ws_status.json"
    path.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    assert latest_equity_ltps() == {}


def test_skips_index_symbols(tmp_path, monkeypatch):
    events = [
        {"ts": "2026-08-12T10:00:00", "topic": "tick.scout", "symbol": "NIFTY", "last_price": 23000},
    ]
    path = _write_snap(tmp_path, events)
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    assert latest_equity_ltps(["NIFTY"]) == {}


def test_ignores_tick_index_topic(tmp_path, monkeypatch):
    events = [
        {"ts": "2026-08-12T10:00:00", "topic": "tick.index", "symbol": "RELIANCE", "last_price": 2500},
    ]
    path = _write_snap(tmp_path, events)
    monkeypatch.setattr("scout.live_quotes._snapshot_path", lambda: path)
    assert latest_equity_ltps(["RELIANCE"]) == {}
