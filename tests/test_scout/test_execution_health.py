"""Tests for scout.execution_health.ws_health."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from scout.execution_health import ws_health


def _snap(**extra):
    base = {
        "connection_state": "connected",
        "generated_at": datetime.now().isoformat(),
        "last_tick_at": datetime.now().isoformat(),
    }
    base.update(extra)
    return base


@patch("scout.execution_health._read_ws_snapshot")
def test_ws_health_ok_with_generated_at(mock_read):
    mock_read.return_value = _snap()
    out = ws_health(market_open=True)
    assert out["ok"] is True
    assert out["connected"] is True
    assert out["stale"] is False


@patch("scout.execution_health._read_ws_snapshot")
def test_ws_health_not_stale_when_only_generated_at(mock_read):
    now = datetime.now()
    mock_read.return_value = {
        "connection_state": "connected",
        "generated_at": now.isoformat(),
    }
    out = ws_health(market_open=True)
    assert out["ok"] is True
    assert out["reason"] == ""


@patch("scout.execution_health._read_ws_snapshot")
def test_ws_health_stale_old_snapshot(mock_read):
    old = datetime.now() - timedelta(seconds=120)
    mock_read.return_value = {
        "connection_state": "connected",
        "generated_at": old.isoformat(),
        "last_tick_at": old.isoformat(),
    }
    out = ws_health(market_open=True)
    assert out["ok"] is False
    assert "stale" in out["reason"]


@patch("scout.execution_health._read_ws_snapshot")
def test_ws_health_missing_timestamps_not_zero_seconds(mock_read):
    mock_read.return_value = {"connection_state": "connected"}
    out = ws_health(market_open=True)
    assert out["ok"] is False
    assert out["reason"] == "ws snapshot missing timestamps"
