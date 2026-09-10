"""Auto-execution registry: milestone hits close; profit target / SL stay manual."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lifecycle.auto_execution.close_on_milestone import (
    CloseOnLossMilestone,
    CloseOnProfitMilestone,
)
from lifecycle.auto_execution.registry import matching_actions, registered_notif_types
from lifecycle.auto_execution.runner import dispatch_auto_execution
from lifecycle.auto_execution.types import AutoExecContext
from lifecycle.zerodha_executor import ZerodhaExecutionError


def test_only_milestones_are_registered():
    assert set(registered_notif_types()) == {
        "LOSS_MILESTONE_HIT",
        "PROFIT_MILESTONE_HIT",
    }


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
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.profit_milestone_config",
        return_value={"enabled": True, "auto_close": True, "pct_of_premium": 5.0},
    )
    assert matching_actions(notif_type) == []


def test_milestone_disabled_or_auto_close_off_matches_nothing(mocker):
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.loss_milestone_config",
        return_value={"enabled": True, "auto_close": False, "pct_of_premium": 5.0},
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.profit_milestone_config",
        return_value={"enabled": True, "auto_close": False, "pct_of_premium": 5.0},
    )
    assert matching_actions("LOSS_MILESTONE_HIT") == []
    assert matching_actions("PROFIT_MILESTONE_HIT") == []


def test_milestone_enabled_matches_close_action(mocker):
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.loss_milestone_config",
        return_value={"enabled": True, "auto_close": True, "pct_of_premium": 5.0},
    )
    actions = matching_actions("LOSS_MILESTONE_HIT")
    assert len(actions) == 1
    assert isinstance(actions[0], CloseOnLossMilestone)


def test_profit_milestone_enabled_matches_close_action(mocker):
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.profit_milestone_config",
        return_value={"enabled": True, "auto_close": True, "pct_of_premium": 10.0},
    )
    actions = matching_actions("PROFIT_MILESTONE_HIT")
    assert len(actions) == 1
    assert isinstance(actions[0], CloseOnProfitMilestone)


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
    broker = MagicMock()
    broker.pending_for_trade.return_value = []
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.BrokerOrderRepo",
        return_value=broker,
    )
    jobs = MagicMock()
    jobs.running_for_trade.return_value = None
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.ZerodhaExecutionJobRepo",
        return_value=jobs,
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


def test_zerodha_in_flight_is_noop(mocker):
    db = MagicMock()
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.TradeRepo.get",
        return_value={"trade_id": "T-1", "status": "ACTIVE"},
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.trade_execution_channel",
        return_value="zerodha",
    )
    broker = MagicMock()
    broker.pending_for_trade.return_value = [{"id": 9}]
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.BrokerOrderRepo",
        return_value=broker,
    )
    kite = mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.close_trade_in_zerodha_async"
    )
    ctx = AutoExecContext(notif_type="LOSS_MILESTONE_HIT", trade_id="T-1")
    assert CloseOnLossMilestone().run(db, ctx) == "in_flight"
    kite.assert_not_called()


def test_zerodha_running_job_is_noop(mocker):
    db = MagicMock()
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.TradeRepo.get",
        return_value={"trade_id": "T-1", "status": "ACTIVE"},
    )
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.trade_execution_channel",
        return_value="zerodha",
    )
    broker = MagicMock()
    broker.pending_for_trade.return_value = []
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.BrokerOrderRepo",
        return_value=broker,
    )
    jobs = MagicMock()
    jobs.running_for_trade.return_value = {"id": 3, "status": "RUNNING"}
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.ZerodhaExecutionJobRepo",
        return_value=jobs,
    )
    kite = mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.close_trade_in_zerodha_async"
    )
    ctx = AutoExecContext(notif_type="LOSS_MILESTONE_HIT", trade_id="T-1")
    assert CloseOnLossMilestone().run(db, ctx) == "in_flight"
    kite.assert_not_called()


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
    broker = MagicMock()
    broker.pending_for_trade.return_value = []
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.BrokerOrderRepo",
        return_value=broker,
    )
    jobs = MagicMock()
    jobs.running_for_trade.return_value = None
    mocker.patch(
        "lifecycle.auto_execution.close_on_milestone.ZerodhaExecutionJobRepo",
        return_value=jobs,
    )
    ctx = AutoExecContext(notif_type="LOSS_MILESTONE_HIT", trade_id="T-1")
    with pytest.raises(ZerodhaExecutionError, match="cannot auto-flatten"):
        CloseOnLossMilestone().run(db, ctx)
