"""Tests for lifecycle/zerodha_executor.py (mocked Kite)."""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest

from lifecycle.zerodha_executor import (
    EXECUTION_CHANNEL_MANUAL,
    EXECUTION_CHANNEL_ZERODHA,
    EXECUTION_PROVIDER_ZERODHA,
    LegFillOutcome,
    ZerodhaExecutionError,
    close_trade_in_zerodha,
    execute_suggestion_in_zerodha,
    execute_supplement_in_zerodha,
    preview_close_execution,
    trade_execution_channel,
    zerodha_execution_enabled,
)
from providers.zerodha.instruments import Instrument
from utils import now_ist


_IST = timezone.utc  # tests only need consistent tz-aware dt


@pytest.fixture
def db_conn():
    db = MagicMock()
    db.commit = MagicMock()
    db.execute = MagicMock(return_value=MagicMock(close=MagicMock()))
    return db


@pytest.fixture(autouse=True)
def _no_inflight_execution_jobs(mocker):
    """MagicMock DB makes job lookups truthy unless explicitly stubbed."""
    job_repo = MagicMock()
    job_repo.running_for_suggestion.return_value = None
    job_repo.running_for_trade.return_value = None
    mocker.patch(
        "database.zerodha_execution_job_repo.ZerodhaExecutionJobRepo",
        return_value=job_repo,
    )


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


@pytest.fixture
def sample_leg():
    return {
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


@pytest.fixture
def sample_suggestion():
    return {
        "suggestion_id": "SUG-1",
        "status": "PENDING",
        "strategy": "LONG_CALL",
        "spot_at_generation": 23000,
        "stop_loss_level": 22800,
    }


def _mock_kite_facade(mocker, mock_instrument):
    kite = MagicMock()
    kite.place_order.return_value = "OID-1"
    kite.order_history.return_value = [{"status": "COMPLETE", "average_price": 99.5}]
    kite.ltp.return_value = {
        "NFO:NIFTY26MAY23000CE": {"last_price": 100.0},
        "NSE:NIFTY 50": {"last_price": 23010},
    }

    facade = MagicMock()
    facade.place_order = kite.place_order
    facade.order_history = kite.order_history
    facade.ltp = kite.ltp
    facade.modify_order = kite.modify_order
    facade.cancel_order = kite.cancel_order
    facade.instruments = MagicMock(return_value=[])
    facade.basket_order_margins.return_value = {"final": {"total": 5000.0}}
    facade.order_margins.return_value = [{"total": 5000.0}]
    facade.margins.return_value = {
        "equity": {
            "available": {"live_balance": 100000.0, "cash": 100000.0},
            "net": 100000.0,
        },
    }
    facade.positions.return_value = {"net": []}

    master = MagicMock()
    master.get_option.return_value = mock_instrument
    master.refresh_if_stale = MagicMock()
    master.refresh = MagicMock()

    mocker.patch(
        "lifecycle.zerodha_executor._build_client",
        return_value=(facade, master),
    )
    mocker.patch(
        "lifecycle.zerodha_executor._refresh_leg_ltp",
        side_effect=lambda _f, _m, leg: 100.0,
    )
    return kite, facade, master


def _patch_entry_gate(mocker):
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion",
        return_value=[],
    )
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills",
        return_value=[],
    )
    mocker.patch(
        "engine.execution_validator.validate_execution",
        return_value=MagicMock(ok=True, reason=lambda: "OK"),
    )
    mocker.patch("lifecycle.zerodha_executor._circuit_breaker_on", return_value=False)


def test_trade_execution_channel_from_provider(db_conn, mocker):
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.has_kite_orders_for_trade",
        return_value=False,
    )
    ch = trade_execution_channel(
        db_conn, {"trade_id": "TRD-1", "execution_provider": "zerodha"},
    )
    assert ch == EXECUTION_CHANNEL_ZERODHA


def test_trade_execution_channel_from_broker_orders(db_conn, mocker):
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.has_kite_orders_for_trade",
        return_value=True,
    )
    ch = trade_execution_channel(
        db_conn, {"trade_id": "TRD-1", "execution_provider": "nse_eod"},
    )
    assert ch == EXECUTION_CHANNEL_ZERODHA


def test_trade_execution_channel_manual(db_conn, mocker):
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.has_kite_orders_for_trade",
        return_value=False,
    )
    ch = trade_execution_channel(
        db_conn, {"trade_id": "TRD-1", "execution_provider": "nse_eod"},
    )
    assert ch == EXECUTION_CHANNEL_MANUAL


def test_validate_execution_receives_circuit_breaker_flag(
    db_conn, mocker, sample_leg, sample_suggestion, mock_instrument,
):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.SuggestionRepo.get", return_value=sample_suggestion)
    mocker.patch("database.models.SuggestionRepo.legs", return_value=[sample_leg])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion", return_value=[])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills", return_value=[])
    mocker.patch("lifecycle.zerodha_executor._circuit_breaker_on", return_value=True)
    mocker.patch("lifecycle.zerodha_executor._run_pre_trade_checks")
    validate = mocker.patch(
        "lifecycle.zerodha_executor.validate_execution",
        return_value=MagicMock(ok=True, reason=lambda: "OK"),
    )
    mocker.patch(
        "lifecycle.zerodha_executor.validate_live_prices",
        return_value=MagicMock(ok=True, reason=lambda: "OK"),
    )
    mocker.patch("lifecycle.zerodha_executor._build_client", return_value=(MagicMock(), MagicMock()))
    mocker.patch(
        "lifecycle.zerodha_executor._live_ltp_map",
        return_value=({1: 100.0}, {1: mock_instrument}),
    )
    from lifecycle.zerodha_executor import _entry_context

    _entry_context(db_conn, "SUG-1", None)
    assert validate.call_args.kwargs["circuit_breaker_active"] is True


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


def test_execute_happy_path_single_leg(
    db_conn, mocker, mock_instrument, sample_leg, sample_suggestion,
):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.SuggestionRepo.get", return_value=sample_suggestion)
    mocker.patch("database.models.SuggestionRepo.legs", return_value=[sample_leg])
    _patch_entry_gate(mocker)

    kite, _, _ = _mock_kite_facade(mocker, mock_instrument)
    mark = mocker.patch("lifecycle.zerodha_executor.mark_executed", return_value="TRD-1")
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.insert", return_value=42)
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.update_status")
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.by_suggestion", return_value=[])

    out = execute_suggestion_in_zerodha(db_conn, "SUG-1")
    assert out.ok
    assert out.trade_id == "TRD-1"
    kite.place_order.assert_called_once()
    mark.assert_called_once()
    assert mark.call_args.kwargs["execution_provider"] == EXECUTION_PROVIDER_ZERODHA
    assert mark.call_args.kwargs["skip_execution_gate"] is True


def test_execute_rolls_back_when_mark_executed_fails(
    db_conn, mocker, mock_instrument, sample_leg, sample_suggestion,
):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.SuggestionRepo.get", return_value=sample_suggestion)
    mocker.patch("database.models.SuggestionRepo.legs", return_value=[sample_leg])
    _patch_entry_gate(mocker)
    _mock_kite_facade(mocker, mock_instrument)
    mocker.patch(
        "lifecycle.zerodha_executor.mark_executed",
        side_effect=ValueError("circuit breaker"),
    )
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.insert", return_value=42)
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.update_status")
    rollback = mocker.patch("lifecycle.zerodha_executor._rollback_entry_legs")

    with pytest.raises(ValueError, match="circuit breaker"):
        execute_suggestion_in_zerodha(db_conn, "SUG-1")
    rollback.assert_called_once()


def test_close_trade_blocks_pending_exit_orders(db_conn, mocker):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.TradeRepo.get", return_value={
        "trade_id": "TRD-1", "status": "OPEN", "suggestion_id": "SUG-1",
    })
    mocker.patch("database.models.TradeRepo.legs_with_suggestion_info", return_value=[{
        "leg_order": 1, "executed": True, "exit_price": None, "action": "BUY",
    }])
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.pending_for_trade",
        return_value=[{"status": "OPEN"}],
    )
    with pytest.raises(ZerodhaExecutionError, match="already in flight"):
        close_trade_in_zerodha(db_conn, "TRD-1")


def test_close_trade_rolls_back_when_second_leg_fails(db_conn, mocker, mock_instrument):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    legs = [
        {
            "leg_order": 1, "executed": True, "exit_price": None, "action": "SELL",
            "option_type": "CE", "symbol": "NIFTY", "expiry_date": date(2026, 5, 28),
            "strike": 23000, "lots": 1, "lot_size": 50,
            "suggested_price": 100, "suggested_price_low": 95, "suggested_price_high": 105,
        },
        {
            "leg_order": 2, "executed": True, "exit_price": None, "action": "BUY",
            "option_type": "PE", "symbol": "NIFTY", "expiry_date": date(2026, 5, 28),
            "strike": 23000, "lots": 1, "lot_size": 50,
            "suggested_price": 90, "suggested_price_low": 85, "suggested_price_high": 95,
        },
    ]
    mocker.patch("database.models.TradeRepo.get", return_value={
        "trade_id": "TRD-1", "status": "OPEN", "suggestion_id": "SUG-1",
    })
    mocker.patch("database.models.TradeRepo.legs_with_suggestion_info", return_value=legs)
    mocker.patch("database.models.SuggestionRepo.get", return_value={"strategy": "LONG_STRADDLE"})
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.pending_for_trade", return_value=[])
    mocker.patch("lifecycle.zerodha_executor._enforce_limit_band")
    inst_ce = mock_instrument
    inst_pe = Instrument(
        instrument_token=2,
        exchange_token=2,
        tradingsymbol="NIFTY26MAY23000PE",
        name="NIFTY",
        expiry=date(2026, 5, 28),
        strike=23000.0,
        tick_size=0.05,
        lot_size=50,
        instrument_type="PE",
        segment="NFO-OPT",
        exchange="NFO",
    )
    kite, facade, master = _mock_kite_facade(mocker, mock_instrument)
    master.get_option.side_effect = lambda sym, exp, strike, opt: (
        inst_pe if opt == "PE" else inst_ce
    )
    kite.ltp.return_value = {
        "NFO:NIFTY26MAY23000CE": {"last_price": 100.0},
        "NFO:NIFTY26MAY23000PE": {"last_price": 90.0},
        "NSE:NIFTY 50": {"last_price": 23010},
    }

    calls = {"n": 0}

    def _place_side_effect(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return LegFillOutcome(
                leg_order=1, fill_price=99.0, fill_time=now_ist(),
                kite_order_id="OID-1", broker_row_id=1,
            )
        raise ZerodhaExecutionError("leg 2 failed")

    mocker.patch("lifecycle.zerodha_executor._place_and_monitor_leg", side_effect=_place_side_effect)
    rollback = mocker.patch("lifecycle.zerodha_executor._rollback_close_legs")

    with pytest.raises(ZerodhaExecutionError, match="leg 2 failed"):
        close_trade_in_zerodha(db_conn, "TRD-1")
    rollback.assert_called_once()


def test_close_trade_happy_path(db_conn, mocker, mock_instrument):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    leg = {
        "leg_order": 1, "executed": True, "exit_price": None, "action": "BUY",
        "option_type": "CE", "symbol": "NIFTY", "expiry_date": date(2026, 5, 28),
        "strike": 23000, "lots": 1, "lot_size": 50,
        "suggested_price": 100, "suggested_price_low": 95, "suggested_price_high": 105,
    }
    mocker.patch("database.models.TradeRepo.get", return_value={
        "trade_id": "TRD-1", "status": "OPEN", "suggestion_id": "SUG-1",
    })
    mocker.patch("database.models.TradeRepo.legs_with_suggestion_info", return_value=[leg])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.pending_for_trade", return_value=[])
    _mock_kite_facade(mocker, mock_instrument)
    mocker.patch(
        "lifecycle.zerodha_executor._place_and_monitor_leg",
        return_value=LegFillOutcome(
            leg_order=1, fill_price=98.0, fill_time=now_ist(),
            kite_order_id="OID-1", broker_row_id=1,
        ),
    )
    close = mocker.patch("lifecycle.zerodha_executor.close_trade_with_fills")
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.by_trade",
        return_value=[{"operation": "EXIT", "status": "COMPLETE"}],
    )

    out = close_trade_in_zerodha(db_conn, "TRD-1")
    assert out.ok
    assert out.trade_id == "TRD-1"
    close.assert_called_once()


def test_supplement_rejects_live_prices_out_of_band(
    db_conn, mocker, mock_instrument, sample_leg,
):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    filled = {**sample_leg, "leg_order": 1, "executed": True, "fill_price": 100.0}
    pending = {**sample_leg, "leg_order": 2, "executed": False}
    mocker.patch("database.models.TradeRepo.get", return_value={
        "trade_id": "TRD-1", "suggestion_id": "SUG-1",
    })
    mocker.patch(
        "database.models.TradeRepo.legs_with_suggestion_info",
        return_value=[filled, pending],
    )
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.pending_for_trade",
        return_value=[],
    )
    mocker.patch(
        "database.models.SuggestionRepo.get",
        return_value={"strategy": "IRON_CONDOR"},
    )
    _mock_kite_facade(mocker, mock_instrument)
    mocker.patch(
        "lifecycle.zerodha_executor._live_ltp_map",
        return_value=({2: 100.0}, {2: mock_instrument}),
    )
    mocker.patch("lifecycle.zerodha_executor._run_pre_trade_checks")
    mocker.patch(
        "lifecycle.zerodha_executor._build_leg_plans",
        return_value=[MagicMock(leg_order=2, transaction_type="BUY")],
    )
    mocker.patch("lifecycle.zerodha_executor._enforce_limit_band")
    mocker.patch(
        "lifecycle.zerodha_executor.validate_live_prices",
        return_value=MagicMock(ok=False, reason=lambda: "CE slipped"),
    )
    place = mocker.patch("lifecycle.zerodha_executor._place_and_monitor_leg")
    with pytest.raises(ZerodhaExecutionError, match="Live prices out of band"):
        execute_supplement_in_zerodha(db_conn, "TRD-1")
    place.assert_not_called()


def test_preview_close_execution_reports_limit_vetoes(db_conn, mocker, mock_instrument):
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    leg = {
        "leg_order": 1, "executed": True, "exit_price": None, "action": "BUY",
        "option_type": "CE", "symbol": "NIFTY", "expiry_date": date(2026, 5, 28),
        "strike": 23000, "lots": 1, "lot_size": 50,
        "suggested_price": 100, "suggested_price_low": 95, "suggested_price_high": 105,
    }
    mocker.patch("database.models.TradeRepo.get", return_value={
        "trade_id": "TRD-1", "status": "OPEN", "suggestion_id": "SUG-1",
    })
    mocker.patch("database.models.TradeRepo.legs_with_suggestion_info", return_value=[leg])
    _mock_kite_facade(mocker, mock_instrument)
    mocker.patch(
        "lifecycle.zerodha_executor._resolve_limit_price",
        return_value=200.0,
    )

    preview = preview_close_execution(db_conn, "TRD-1")
    assert preview.operation == "EXIT"
    assert preview.all_limits_in_band is False
    assert preview.limit_vetoes


def test_pre_trade_checks_block_insufficient_funds(mocker, mock_instrument):
    from lifecycle.zerodha_executor import _run_pre_trade_checks
    from providers.zerodha.execution_checks import MarginCheckResult

    mocker.patch(
        "lifecycle.zerodha_executor.build_order_margin_params",
        return_value=[{"variety": "regular"}],
    )
    mocker.patch(
        "lifecycle.zerodha_executor.check_margin_for_orders",
        return_value=MarginCheckResult(
            ok=False, required=20000.0, available=1000.0,
            message="Insufficient funds in Zerodha: need ~₹21,000",
        ),
    )
    with pytest.raises(ZerodhaExecutionError, match="Insufficient funds"):
        _run_pre_trade_checks(
            MagicMock(), [{"leg_order": 1, "action": "BUY"}],
            {1: mock_instrument}, [{"leg_order": 1, "action": "BUY"}],
            None, mode="entry", live_map={1: 100.0},
        )


def test_preview_includes_margin_snapshot(
    db_conn, mocker, sample_leg, sample_suggestion, mock_instrument,
):
    from lifecycle.zerodha_executor import preview_suggestion_execution
    from providers.zerodha.execution_checks import MarginCheckResult

    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.SuggestionRepo.get", return_value=sample_suggestion)
    mocker.patch("database.models.SuggestionRepo.legs", return_value=[sample_leg])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion", return_value=[])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills", return_value=[])
    mocker.patch("lifecycle.zerodha_executor._circuit_breaker_on", return_value=False)
    mocker.patch(
        "lifecycle.zerodha_executor.validate_execution",
        return_value=MagicMock(ok=True, reason=lambda: "OK"),
    )
    mocker.patch(
        "lifecycle.zerodha_executor.validate_live_prices",
        return_value=MagicMock(ok=True, reason=lambda: "OK"),
    )
    mocker.patch("lifecycle.zerodha_executor._build_client", return_value=(MagicMock(), MagicMock()))
    mocker.patch(
        "lifecycle.zerodha_executor._live_ltp_map",
        return_value=({1: 100.0}, {1: mock_instrument}),
    )
    mocker.patch(
        "lifecycle.zerodha_executor._run_pre_trade_checks",
        return_value=MarginCheckResult(
            ok=True, required=18450.0, available=120000.0,
        ),
    )
    mocker.patch("lifecycle.zerodha_executor._spot_ltp", return_value=23010.0)
    preview = preview_suggestion_execution(db_conn, "SUG-1")
    body = preview.to_dict()
    assert body["margin_required"] == 18450.0
    assert body["margin_available"] == 120000.0
    assert body["margin_ok"] is True
