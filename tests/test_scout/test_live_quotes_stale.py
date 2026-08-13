"""Tests for stale live quote filtering."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from scout.live_quotes import fresh_equity_ltp, latest_equity_ltps


def test_latest_equity_ltps_rejects_old_ticks(tmp_path, mocker):
    old_ts = (datetime.now() - timedelta(seconds=120)).isoformat()
    snap = {
        "generated_at": datetime.now().isoformat(),
        "recent_events": [
            {
                "topic": "tick.scout",
                "symbol": "RELIANCE",
                "last_price": 2500.0,
                "ts": old_ts,
            },
        ],
    }
    snap_path = tmp_path / "ws_status.json"
    snap_path.write_text(__import__("json").dumps(snap), encoding="utf-8")
    mocker.patch("scout.live_quotes._snapshot_path", return_value=snap_path)

    assert latest_equity_ltps(["RELIANCE"], max_age_seconds=45) == {}
    assert fresh_equity_ltp("RELIANCE", max_age_seconds=45) is None


def test_latest_equity_ltps_accepts_fresh_ticks(tmp_path, mocker):
    fresh_ts = datetime.now().isoformat()
    snap = {
        "generated_at": fresh_ts,
        "recent_events": [
            {
                "topic": "tick.scout",
                "symbol": "TCS",
                "last_price": 4000.0,
                "ts": fresh_ts,
            },
        ],
    }
    snap_path = tmp_path / "ws_status.json"
    snap_path.write_text(__import__("json").dumps(snap), encoding="utf-8")
    mocker.patch("scout.live_quotes._snapshot_path", return_value=snap_path)

    quotes = latest_equity_ltps(["TCS"], max_age_seconds=45)
    assert quotes["TCS"]["ltp"] == 4000.0
    assert fresh_equity_ltp("TCS", max_age_seconds=45) == 4000.0
