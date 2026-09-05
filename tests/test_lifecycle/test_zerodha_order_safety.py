"""Safety tests — buy/sell, call/put identity, and execution sequencing."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from lifecycle.zerodha_executor import (
    ZerodhaExecutionError,
    _assert_instrument_matches_leg,
    _build_leg_plans,
    _kite_order_is_dead,
    _transaction_type_for_leg,
    execute_suggestion_in_zerodha,
)
from providers.zerodha.instruments import Instrument


def _inst(opt: str, strike: float = 23000.0) -> Instrument:
    return Instrument(
        instrument_token=1,
        exchange_token=1,
        tradingsymbol=f"NIFTY26MAY{int(strike)}{opt}",
        name="NIFTY",
        expiry=date(2026, 5, 28),
        strike=strike,
        tick_size=0.05,
        lot_size=50,
        instrument_type=opt,
        segment="NFO-OPT",
        exchange="NFO",
    )


def _leg(lo: int, action: str, opt: str, strike: float = 23000.0) -> dict:
    return {
        "leg_order": lo,
        "action": action,
        "option_type": opt,
        "symbol": "NIFTY",
        "expiry_date": date(2026, 5, 28),
        "strike": strike,
        "lots": 1,
        "lot_size": 50,
        "suggested_price": 100,
        "suggested_price_low": 95,
        "suggested_price_high": 105,
    }


def test_rollback_reverses_first_leg_partial_when_completed_empty(mocker):
    from lifecycle.zerodha_executor import (
        LegFillOutcome,
        _rollback_filled_legs,
    )
    from utils import now_ist

    facade = MagicMock()
    db = MagicMock()
    reverse = mocker.patch("lifecycle.zerodha_executor._reverse_partial_leg")
    mocker.patch(
        "database.broker_order_repo.BrokerOrderRepo.rollback_complete_for_leg",
        return_value=False,
    )
    leg = _leg(1, "BUY", "CE")
    inst = _inst("CE")
    partial = LegFillOutcome(
        leg_order=1,
        fill_price=99.0,
        fill_time=now_ist(),
        kite_order_id="OID-P",
        broker_row_id=1,
        filled_quantity=25,
        planned_quantity=50,
    )
    _rollback_filled_legs(
        facade, db,
        suggestion_id="SUG-1",
        trade_id=None,
        legs_by_order={1: leg},
        completed=[],
        mode="entry",
        inst_map={1: inst},
        partial_on_fail=partial,
    )
    reverse.assert_called_once()
    assert reverse.call_args.kwargs["partial"].filled_quantity == 25


class TestTransactionType:
    def test_entry_uses_leg_action(self):
        assert _transaction_type_for_leg(_leg(1, "BUY", "CE"), "entry") == "BUY"
        assert _transaction_type_for_leg(_leg(1, "SELL", "PE"), "entry") == "SELL"

    def test_close_flips_side(self):
        assert _transaction_type_for_leg(_leg(1, "SELL", "CE"), "close") == "BUY"
        assert _transaction_type_for_leg(_leg(1, "BUY", "PE"), "close") == "SELL"

    def test_rollback_reverses_entry(self):
        assert _transaction_type_for_leg(_leg(1, "BUY", "CE"), "rollback") == "SELL"
        assert _transaction_type_for_leg(_leg(1, "SELL", "PE"), "rollback") == "BUY"

    def test_invalid_action_rejected(self):
        with pytest.raises(ZerodhaExecutionError, match="invalid action"):
            _transaction_type_for_leg(_leg(1, "HOLD", "CE"), "entry")


class TestDeadOrderRetry:
    def test_rejected_order_is_dead(self):
        facade = MagicMock()
        facade.order_history.return_value = [{"status": "REJECTED"}]
        assert _kite_order_is_dead(facade, "OID-1") is True

    def test_open_order_is_not_dead(self):
        facade = MagicMock()
        facade.order_history.return_value = [{"status": "OPEN"}]
        assert _kite_order_is_dead(facade, "OID-1") is False


class TestInstrumentMatch:
    def test_matching_instrument_passes(self):
        leg = _leg(1, "BUY", "CE")
        _assert_instrument_matches_leg(leg, _inst("CE"))

    def test_option_type_mismatch_rejected(self):
        with pytest.raises(ZerodhaExecutionError, match="option type mismatch"):
            _assert_instrument_matches_leg(_leg(1, "BUY", "CE"), _inst("PE"))

    def test_strike_mismatch_rejected(self):
        with pytest.raises(ZerodhaExecutionError, match="strike mismatch"):
            _assert_instrument_matches_leg(_leg(1, "BUY", "CE", 23100), _inst("CE", 23000))


class TestBuildLegPlans:
    def test_close_plans_flip_transaction_type(self):
        legs = [_leg(1, "SELL", "CE"), _leg(2, "BUY", "PE")]
        inst_map = {1: _inst("CE"), 2: _inst("PE", 22900)}
        live = {1: 100.0, 2: 90.0}
        plans = _build_leg_plans(legs, inst_map, live, None, mode="close", strategy="LONG_STRADDLE")
        by_lo = {p.leg_order: p for p in plans}
        assert by_lo[1].transaction_type == "BUY"
        assert by_lo[2].transaction_type == "SELL"


def test_multi_leg_entry_order_and_side(mock_db, mocker):
    """Spread entry: BUY hedge first, then SELL short — with correct Kite sides."""
    legs = [
        _leg(1, "SELL", "CE", 23100),
        _leg(2, "BUY", "CE", 23000),
    ]
    mocker.patch("lifecycle.zerodha_executor.zerodha_execution_enabled", return_value=True)
    mocker.patch("database.models.SuggestionRepo.get", return_value={
        "suggestion_id": "SUG-1",
        "status": "PENDING",
        "strategy": "BEAR_CALL_SPREAD",
        "spot_at_generation": 23000,
        "stop_loss_level": 22800,
    })
    mocker.patch("database.models.SuggestionRepo.legs", return_value=legs)
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
    mocker.patch("lifecycle.zerodha_executor._enforce_limit_band")
    mocker.patch("lifecycle.zerodha_executor.mark_executed", return_value="TRD-1")
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.by_suggestion", return_value=[])

    inst_buy = _inst("CE", 23000)
    inst_sell = _inst("CE", 23100)
    master = MagicMock()
    master.get_option.side_effect = lambda sym, exp, strike, opt: (
        inst_buy if strike == 23000 else inst_sell
    )
    master.refresh_if_stale = MagicMock()
    master.refresh = MagicMock()

    kite = MagicMock()
    kite.place_order.return_value = "OID-1"
    kite.order_history.return_value = [{"status": "COMPLETE", "average_price": 99.5}]
    kite.ltp.return_value = {
        "NFO:NIFTY26MAY23000CE": {"last_price": 100.0},
        "NFO:NIFTY26MAY23100CE": {"last_price": 80.0},
        "NSE:NIFTY 50": {"last_price": 23010},
    }
    facade = MagicMock()
    facade.place_order = kite.place_order
    facade.order_history = kite.order_history
    facade.ltp = kite.ltp
    facade.modify_order = kite.modify_order
    facade.cancel_order = kite.cancel_order
    facade.basket_order_margins.return_value = {"final": {"total": 8000.0}}
    facade.margins.return_value = {
        "equity": {"available": {"live_balance": 100000.0}, "net": 100000.0},
    }
    facade.positions.return_value = {"net": []}

    mocker.patch("lifecycle.zerodha_executor._build_client", return_value=(facade, master))
    mocker.patch("lifecycle.zerodha_executor._refresh_leg_ltp", side_effect=lambda _f, _m, leg: 100.0)
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.insert", side_effect=lambda row: row["leg_order"])
    mocker.patch("database.broker_order_repo.BrokerOrderRepo.update_status")

    execute_suggestion_in_zerodha(mock_db, "SUG-1")

    assert kite.place_order.call_count == 2
    first = kite.place_order.call_args_list[0].kwargs
    second = kite.place_order.call_args_list[1].kwargs
    assert first["transaction_type"] == "BUY"
    assert first["tradingsymbol"] == inst_buy.tradingsymbol
    assert second["transaction_type"] == "SELL"
    assert second["tradingsymbol"] == inst_sell.tradingsymbol
