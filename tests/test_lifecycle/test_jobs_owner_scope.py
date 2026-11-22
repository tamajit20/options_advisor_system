"""Scheduler/lifecycle jobs must keep working under fail-closed owner scope."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _assert_admin_all_in_source(*rel_paths: str) -> None:
    root = Path(__file__).resolve().parents[2]
    for rel in rel_paths:
        text = (root / rel).read_text(encoding="utf-8")
        assert "open_trades(admin_all=True)" in text, (
            f"{rel} must call open_trades(admin_all=True) for system-wide scans"
        )
        # Guard against a bare open_trades() regressing.
        for line in text.splitlines():
            if "open_trades(" not in line:
                continue
            if "admin_all=True" in line or "owner_user_id=" in line:
                continue
            if line.strip().startswith("#"):
                continue
            # Method definition is fine.
            if "def open_trades" in line:
                continue
            raise AssertionError(
                f"{rel}: bare open_trades() call is not fail-closed: {line.strip()}"
            )


def test_trade_scanning_jobs_use_admin_all_in_source():
    _assert_admin_all_in_source(
        "lifecycle/exit_orchestrator.py",
        "lifecycle/live_risk_monitor.py",
        "lifecycle/event_eve_review.py",
        "lifecycle/intraday_sl_fallback.py",
        "lifecycle/trade_greeks_job.py",
        "lifecycle/snapshot_orchestrator.py",
        "lifecycle/suggestion_engine.py",
    )


def test_trade_greeks_job_uses_admin_all(mocker):
    open_mock = mocker.patch(
        "lifecycle.trade_greeks_job.TradeRepo.open_trades", return_value=[],
    )
    mocker.patch("lifecycle.trade_greeks_job.TradeGreeksRepo")
    mocker.patch("lifecycle.trade_greeks_job.SpotEodRepo")
    mocker.patch("lifecycle.trade_greeks_job.IvHistoryRepo")
    mocker.patch(
        "lifecycle.trade_greeks_job.effective_bhav_end_date",
        return_value=date(2026, 5, 4),
    )
    from lifecycle.trade_greeks_job import run_trade_greeks_update

    assert run_trade_greeks_update(MagicMock()) == 0
    open_mock.assert_called_with(admin_all=True)


def test_auto_flatten_passes_trade_owner(mocker):
    from lifecycle.auto_execution.close_on_milestone import flatten_open_trade
    from lifecycle.auto_execution.types import AutoExecContext
    from lifecycle.zerodha_executor import EXECUTION_CHANNEL_ZERODHA

    db = MagicMock()
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.TradeRepo.get",
        return_value={
            "trade_id": "TRD-1",
            "status": "ACTIVE",
            "owner_user_id": 42,
        },
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.trade_execution_channel",
        return_value=EXECUTION_CHANNEL_ZERODHA,
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.BrokerOrderRepo.has_entry_kite_fills_for_trade",
        return_value=True,
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone._flatten_already_moving",
        return_value=False,
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.zerodha_execution_ready",
        return_value=True,
    )
    close = mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.close_trade_in_zerodha_async",
    )
    ctx = AutoExecContext(notif_type="LOSS_MILESTONE_HIT", trade_id="TRD-1")
    assert flatten_open_trade(db, ctx) == "zerodha"
    assert close.call_args.kwargs.get("actor_user_id") == 42


def test_capital_insert_requires_owner():
    from database.models import ZerodhaCapitalSnapshotRepo
    from database.owner_scope import OwnerScopeError

    db = MagicMock()
    repo = ZerodhaCapitalSnapshotRepo(db)
    snap = {
        "available": True,
        "user_id": "AB1234",
        "net": 1000.0,
        "usable_balance": 900.0,
        "available_cash": 800.0,
        "live_balance": 900.0,
        "equity": {},
    }
    with pytest.raises(OwnerScopeError):
        repo.insert_if_changed(snap, owner_user_id=None)


def test_zerodha_capital_job_passes_app_user_id(mocker):
    mocker.patch(
        "lifecycle.zerodha_capital_snapshot.in_nse_session", return_value=True,
    )
    mocker.patch(
        "database.zerodha_session_repo.ZerodhaSessionRepo.list_user_ids_with_token",
        return_value=[7, 9],
    )
    fetch = mocker.patch(
        "providers.zerodha.account_snapshot.fetch_account_snapshot",
        return_value={"available": True, "capital_persisted": True},
    )
    from lifecycle.zerodha_capital_snapshot import run_zerodha_capital_snapshot

    assert run_zerodha_capital_snapshot(MagicMock()) == 2
    assert [c.kwargs.get("app_user_id") for c in fetch.call_args_list] == [7, 9]
