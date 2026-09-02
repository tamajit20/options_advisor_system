"""Tests for lifecycle/zerodha_executor.py (mocked Kite)."""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from lifecycle.zerodha_executor import (
    ZerodhaExecutionError,
    execute_suggestion_in_zerodha,
    zerodha_execution_enabled,
)
from providers.zerodha.instruments import Instrument


_IST = timezone.utc  # tests only need consistent tz-aware dt


@pytest.fixture
def db_conn():
    db = MagicMock()
    db.commit = MagicMock()
    db.execute = MagicMock(return_value=MagicMock(close=MagicMock()))
    return db


@pytest.fixture
def mock_instrument():
    return Instrument(
        instrument_token=1,
        exchange_token=1,
        tradingsymbol="NIFTY26MAY23000CE",
        name="NIFTY",
        expiry=date(2026, 5, 28),
        strike=23000.0,
        tick_size=0.05,
        lot_size=50,
        instrument_type="CE",
        segment="NFO-OPT",
        exchange="NFO",
    )


def test_execution_disabled_without_config(db_conn, mocker):
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_config_enabled",
        return_value=False,
    )
    mocker.patch(
        "lifecycle.zerodha_executor.zerodha_execution_runtime_enabled",
        return_value=True,
    )
    assert not zerodha_execution_enabled(db_conn)
    with pytest.raises(ZerodhaExecutionError, match="disabled"):
        execute_suggestion_in_zerodha(db_conn, "SUG-1")


def test_execute_rejects_non_pending(db_conn, mocker, mock_instrument):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.SuggestionRepo.get", return_value={
        "suggestion_id": "SUG-1",
        "status": "EXECUTED",
        "strategy": "LONG_CALL",
    })
    mocker.patch("database.models.SuggestionRepo.legs", return_value=[{
        "leg_order": 1,
        "action": "BUY",
        "option_type": "CE",
        "symbol": "NIFTY",
        "expiry_date": date(2026, 5, 28),
        "strike": 23000,
        "lots": 1,
        "lot_size": 50,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion", return_value=[])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills", return_value=[])
    with pytest.raises(ZerodhaExecutionError, match="PENDING"):
        execute_suggestion_in_zerodha(db_conn, "SUG-1")


def test_execute_blocks_orphan_broker_fills(db_conn, mocker):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.SuggestionRepo.get", return_value={
        "suggestion_id": "SUG-1",
        "status": "PENDING",
        "strategy": "LONG_CALL",
    })
    mocker.patch("database.models.SuggestionRepo.legs", return_value=[{
        "leg_order": 1,
        "action": "BUY",
        "option_type": "CE",
        "symbol": "NIFTY",
        "expiry_date": date(2026, 5, 28),
        "strike": 23000,
        "lots": 1,
        "lot_size": 50,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion", return_value=[])
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills",
        return_value=[{"leg_order": 1, "status": "COMPLETE"}],
    )
    with pytest.raises(ZerodhaExecutionError, match="Prior Zerodha entry"):
        execute_suggestion_in_zerodha(db_conn, "SUG-1")


def test_execute_blocks_circuit_breaker_before_orders(db_conn, mocker):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.SuggestionRepo.get", return_value={
        "suggestion_id": "SUG-1",
        "status": "PENDING",
        "strategy": "LONG_CALL",
    })
    mocker.patch("database.models.SuggestionRepo.legs", return_value=[{
        "leg_order": 1,
        "action": "BUY",
        "option_type": "CE",
        "symbol": "NIFTY",
        "expiry_date": date(2026, 5, 28),
        "strike": 23000,
        "lots": 1,
        "lot_size": 50,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion", return_value=[])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills", return_value=[])
    mocker.patch("lifecycle.zerodha_executor._circuit_breaker_on", return_value=True)
    mocker.patch(
        "engine.execution_validator.validate_execution",
        return_value=MagicMock(ok=False, reason=lambda: "circuit breaker"),
    )
    with pytest.raises(ZerodhaExecutionError, match="circuit breaker"):
        execute_suggestion_in_zerodha(db_conn, "SUG-1")


def test_execute_happy_path_single_leg(db_conn, mocker, mock_instrument):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    leg = {
        "leg_order": 1,
        "action": "BUY",
        "option_type": "CE",
        "symbol": "NIFTY",
        "expiry_date": date(2026, 5, 28),
        "strike": 23000,
        "lots": 1,
        "lot_size": 50,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }
    suggestion = {
        "suggestion_id": "SUG-1",
        "status": "PENDING",
        "strategy": "LONG_CALL",
        "spot_at_generation": 23000,
        "stop_loss_level": 22800,
    }
    mocker.patch("database.models.SuggestionRepo.get", return_value=suggestion)
    mocker.patch("database.models.SuggestionRepo.legs", return_value=[leg])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion", return_value=[])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills", return_value=[])
    mocker.patch("engine.execution_validator.validate_execution", return_value=MagicMock(ok=True, vetoes=[]))

    kite = MagicMock()
    kite.place_order.return_value = "OID-1"
    kite.order_history.return_value = [{"status": "COMPLETE", "average_price": 99.5}]
    kite.ltp.return_value = {"NFO:NIFTY26MAY23000CE": {"last_price": 100.0}, "NSE:NIFTY 50": {"last_price": 23010}}

    facade = MagicMock()
    facade.place_order = kite.place_order
    facade.order_history = kite.order_history
    facade.ltp = kite.ltp
    facade.modify_order = kite.modify_order
    facade.cancel_order = kite.cancel_order
    facade.instruments = MagicMock(return_value=[])

    master = MagicMock()
    master.get_option.return_value = mock_instrument
    master.refresh_if_stale = MagicMock()
    master.refresh = MagicMock()

    mocker.patch("lifecycle.zerodha_executor._build_client", return_value=(facade, master))
    mocker.patch("lifecycle.zerodha_executor._refresh_leg_ltp", side_effect=lambda _f, _m, leg: 100.0)
    mocker.patch("lifecycle.zerodha_executor.mark_executed", return_value="TRD-1")
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.insert", return_value=42)
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.update_status")
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.by_suggestion", return_value=[])

    out = execute_suggestion_in_zerodha(db_conn, "SUG-1")
    assert out.ok
    assert out.trade_id == "TRD-1"
    kite.place_order.assert_called_once()
