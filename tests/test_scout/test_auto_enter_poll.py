"""Tests for auto-enter pending poll."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scout.auto_trader import try_auto_enter_pending_signals


def test_try_auto_enter_pending_signals(mocker):
    db = MagicMock()
    mocker.patch(
        "scout.auto_trader.reload_scout_settings",
        return_value={"auto_execute_signals": True},
    )
    mocker.patch("scout.auto_trader.is_market_open", return_value=True)
    mocker.patch("scout.auto_trader.SCOUT_CONFIG", {"enabled": True})
    sig_repo = MagicMock()
    sig_repo.signal_ids_without_trade.return_value = [10, 11]
    mocker.patch("scout.auto_trader.ScoutSignalRepo", return_value=sig_repo)
    mock_exec = mocker.patch(
        "scout.auto_trader.try_auto_execute_signal",
        side_effect=[{"trade_id": 1}, None],
    )

    out = try_auto_enter_pending_signals(db, spot_lookup=lambda s: 100.0)
    assert len(out) == 1
    assert mock_exec.call_count == 2
