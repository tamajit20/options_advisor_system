"""Tests for lifecycle.intraday_sl_fallback — WS-down SL fallback."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from lifecycle.intraday_sl_fallback import run_intraday_sl_fallback


def test_skips_when_ws_healthy():
    db = MagicMock()
    with patch(
        "lifecycle.intraday_sl_fallback._should_run",
        return_value=(False, "ws_healthy"),
    ):
        assert run_intraday_sl_fallback(db) == 0
    db.commit.assert_not_called()


def test_runs_when_ws_unhealthy_and_trades_exist():
    db = MagicMock()
    trade = {"trade_id": "T-1", "status": "ACTIVE", "trade_name": "Test"}
    with patch("lifecycle.intraday_sl_fallback._should_run", return_value=(True, "stale")), \
         patch("lifecycle.intraday_sl_fallback.TradeRepo") as tr_cls, \
         patch("lifecycle.intraday_sl_fallback._evaluate_trade", return_value=None):
        tr_cls.return_value.open_trades.return_value = [trade]
        n = run_intraday_sl_fallback(
            db, trade_date=datetime(2026, 5, 5).date(), provider=MagicMock(),
        )
    assert n == 0
    db.commit.assert_called_once()
