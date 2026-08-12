"""Tests for scout scanner and orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scout.market_data import ScoutMarketError
from scout.orchestrator import run_scout_scan
from scout.scanner import scan_watchlist


def test_scan_watchlist_skips_symbol_on_market_error():
    mkt = MagicMock()
    mkt.benchmark_pct_from_open.return_value = 0.1
    mkt.watchlist.return_value = ["RELIANCE", "TCS"]
    mkt.minute_bars.side_effect = [
        ScoutMarketError("no data"),
        ([], {"open": 4000, "high": 4010, "low": 3990, "ltp": 4005}),
    ]
    rows, count = scan_watchlist(mkt)
    assert count == 2
    assert rows == []


def test_run_scout_scan_disabled(mocker):
    mocker.patch("scout.orchestrator.SCOUT_CONFIG", {"enabled": False})
    db = MagicMock()
    assert run_scout_scan(db) == 0


def test_run_scout_scan_market_closed(mocker):
    mocker.patch("scout.orchestrator.SCOUT_CONFIG", {"enabled": True})
    mocker.patch("scout.orchestrator.is_market_open", return_value=False)
    assert run_scout_scan(MagicMock()) == 0


def test_run_scout_scan_zerodha_not_ready(mocker):
    mocker.patch("scout.orchestrator.SCOUT_CONFIG", {"enabled": True})
    mocker.patch("scout.orchestrator.is_market_open", return_value=True)
    mocker.patch("scout.orchestrator.zerodha_ready", return_value=(False, "login required"))
    assert run_scout_scan(MagicMock()) == 0


def test_run_scout_scan_success_inserts_signals(mocker):
    import scout.orchestrator as orch

    mocker.patch.object(orch, "SCOUT_CONFIG", {"enabled": True})
    mocker.patch.object(orch, "is_market_open", return_value=True)
    mocker.patch.object(orch, "zerodha_ready", return_value=(True, "ok"))
    mocker.patch.object(
        orch,
        "scan_watchlist",
        return_value=([{
            "symbol": "TCS",
            "action": "BUY",
            "signal_type": "OR_BREAK_UP",
            "reason": "test",
            "ltp": 4000.0,
            "invalidation": 3950.0,
            "strength": "MEDIUM",
            "meta": {},
        }], 1),
    )
    mocker.patch.object(orch, "ScoutMarketData", MagicMock())
    mock_sig = MagicMock()
    mock_sig.insert.return_value = 42
    mock_log = MagicMock()
    mocker.patch.object(orch, "ScoutSignalRepo", return_value=mock_sig)
    mocker.patch.object(orch, "ScoutScanLogRepo", return_value=mock_log)
    db = MagicMock()
    n = run_scout_scan(db)
    assert n == 1
    mock_sig.insert.assert_called_once()
    db.commit.assert_called()
