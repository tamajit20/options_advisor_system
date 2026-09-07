"""Auto-execution registry: only LOSS_MILESTONE_HIT closes; profit/entry stay manual."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lifecycle.auto_execution.close_on_milestone import CloseOnLossMilestone
from lifecycle.auto_execution.registry import matching_actions, registered_notif_types
from lifecycle.auto_execution.runner import dispatch_auto_execution
from lifecycle.auto_execution.types import AutoExecContext
from lifecycle.zerodha_executor import ZerodhaExecutionError


def test_only_loss_milestone_is_registered():
    assert registered_notif_types() == ("LOSS_MILESTONE_HIT",)


@pytest.mark.parametrize("notif_type", [
    "TARGET_HIT",
    "LOSS_LIMIT_HIT",
    "SL_TRIGGER",
    "PROFIT_FLOOR_HIT",
    "PRE_BREACH_WARNING",
])
def test_manual_alerts_have_no_auto_action(notif_type, mocker):
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.loss_milestone_config",
        return_value={"enabled": True, "auto_close": True, "pct_of_premium": 5.0},
    )
    assert matching_actions(notif_type) == []


def test_milestone_disabled_or_auto_close_off_matches_nothing(mocker):
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.loss_milestone_config",
        return_value={"enabled": True, "auto_close": False, "pct_of_premium": 5.0},
    )
    assert matching_actions("LOSS_MILESTONE_HIT") == []


def test_milestone_enabled_matches_close_action(mocker):
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.loss_milestone_config",
        return_value={"enabled": True, "auto_close": True, "pct_of_premium": 5.0},
    )
    actions = matching_actions("LOSS_MILESTONE_HIT")
    assert len(actions) == 1
    assert isinstance(actions[0], CloseOnLossMilestone)


def test_dispatch_skips_thread_when_no_action(mocker):
    started = mocker.patch("lifecycle.auto_execution.runner.threading.Thread")
    dispatch_auto_execution(AutoExecContext(
        notif_type="TARGET_HIT", trade_id="T-1",
    ))
    started.assert_not_called()


def test_already_closed_is_noop(mocker):
    db = MagicMock()
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.TradeRepo.get",
        return_value={"trade_id": "T-1", "status": "CLOSED"},
    )
    ctx = AutoExecContext(notif_type="LOSS_MILESTONE_HIT", trade_id="T-1")
    assert CloseOnLossMilestone().run(db, ctx) == "already_closed"


def test_manual_books_live_exits(mocker):
    db = MagicMock()
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.TradeRepo.get",
        return_value={"trade_id": "T-1", "status": "ACTIVE"},
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.trade_execution_channel",
        return_value="manual",
    )
    close = mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.close_trade_with_fills"
    )
    kite = mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.close_trade_in_zerodha_async"
    )
    exits = [{"leg_order": 1, "exit_price": 130.0}]
    ctx = AutoExecContext(
        notif_type="LOSS_MILESTONE_HIT", trade_id="T-1", exits=exits,
    )
    assert CloseOnLossMilestone().run(db, ctx) == "manual"
    close.assert_called_once_with(db, "T-1", exits)
    kite.assert_not_called()


def test_zerodha_flattens_on_kite(mocker):
    db = MagicMock()
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.TradeRepo.get",
        return_value={"trade_id": "T-1", "status": "ACTIVE",
                      "execution_provider": "zerodha"},
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.trade_execution_channel",
        return_value="zerodha",
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.zerodha_execution_ready",
        return_value=True,
    )
    kite = mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.close_trade_in_zerodha_async"
    )
    close = mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.close_trade_with_fills"
    )
    ctx = AutoExecContext(notif_type="LOSS_MILESTONE_HIT", trade_id="T-1")
    assert CloseOnLossMilestone().run(db, ctx) == "zerodha"
    kite.assert_called_once_with(db, "T-1")
    close.assert_not_called()


def test_zerodha_not_ready_raises(mocker):
    db = MagicMock()
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.TradeRepo.get",
        return_value={"trade_id": "T-1", "status": "ACTIVE"},
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.trade_execution_channel",
        return_value="zerodha",
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.zerodha_execution_ready",
        return_value=False,
    )
    ctx = AutoExecContext(notif_type="LOSS_MILESTONE_HIT", trade_id="T-1")
    with pytest.raises(ZerodhaExecutionError, match="cannot auto-flatten"):
        CloseOnLossMilestone().run(db, ctx)
