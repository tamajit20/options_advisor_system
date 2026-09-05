"""Tests for providers/zerodha/execution_checks.py"""

from unittest.mock import MagicMock

import pytest

from providers.zerodha.execution_checks import (
    _required_from_margin_response,
    build_order_margin_params,
    check_exposure_conflicts,
    check_margin_for_orders,
)
from providers.zerodha.instruments import Instrument
from datetime import date


def _inst(sym: str) -> Instrument:
    return Instrument(
        instrument_token=1,
        exchange_token=1,
        tradingsymbol=sym,
        name="NIFTY",
        expiry=date(2026, 5, 28),
        strike=23000.0,
        tick_size=0.05,
        lot_size=50,
        instrument_type="CE",
        segment="NFO-OPT",
        exchange="NFO",
    )


def test_exposure_blocks_duplicate_long_entry():
    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": 50}],
    }
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = check_exposure_conflicts(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert not out.ok


def test_exposure_allows_buy_to_cover_short():
    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": -50}],
    }
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = check_exposure_conflicts(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert out.ok


def test_exposure_skipped_for_supplement():
    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": 50}],
    }
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = check_exposure_conflicts(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
        allow_existing_positions=True,
    )
    assert out.ok
    facade.positions.assert_not_called()


def test_margin_params_reject_zero_qty():
    from dataclasses import replace
    inst = replace(_inst("NIFTY26MAY23000CE"), lot_size=0)
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 0}]
    with pytest.raises(ValueError, match="quantity"):
        build_order_margin_params(
            legs, {1: inst},
            transaction_fn=lambda leg: "BUY",
            limit_fn=lambda lo, inst, txn: 100.0,
            product="NRML",
            variety="regular",
        )


def test_required_from_basket_final_total():
    assert _required_from_margin_response({"final": {"total": 18450.5}}) == 18450.5
    assert _required_from_margin_response({"data": {"initial": {"total": 100.0}}}) == 100.0
    assert _required_from_margin_response([{"total": 10}, {"total": 5}]) == 15.0
    assert _required_from_margin_response({}) is None


def test_margin_blocks_when_available_below_required():
    facade = MagicMock()
    facade.basket_order_margins.return_value = {"final": {"total": 20000.0}}
    facade.margins.return_value = {
        "equity": {"available": {"live_balance": 5000.0}},
    }
    out = check_margin_for_orders(facade, [{"variety": "regular"}])
    assert not out.ok
    assert out.required == 20000.0
    assert out.available == 5000.0
    assert "Insufficient funds" in out.message


def test_margin_ok_when_usable_covers_required_plus_buffer():
    facade = MagicMock()
    facade.basket_order_margins.return_value = {"final": {"total": 20000.0}}
    facade.margins.return_value = {
        "equity": {"available": {"live_balance": 25000.0}},
    }
    out = check_margin_for_orders(facade, [{"variety": "regular"}])
    assert out.ok
    assert out.required == 20000.0
    assert out.available == 25000.0


def test_margin_fail_closed_when_balance_unreadable():
    facade = MagicMock()
    facade.basket_order_margins.return_value = {"final": {"total": 20000.0}}
    facade.margins.side_effect = RuntimeError("kite timeout")
    out = check_margin_for_orders(facade, [{"variety": "regular"}])
    assert not out.ok
    assert "could not read Zerodha account balance" in out.message


def test_margin_fail_closed_when_required_unknown():
    facade = MagicMock()
    facade.basket_order_margins.side_effect = RuntimeError("no basket")
    facade.order_margins.side_effect = RuntimeError("no orders")
    out = check_margin_for_orders(facade, [{"variety": "regular"}])
    assert not out.ok
    assert "could not estimate required margin" in out.message


def test_margin_uses_fallback_when_kite_required_missing():
    facade = MagicMock()
    facade.basket_order_margins.return_value = {}
    facade.margins.return_value = {
        "equity": {"available": {"live_balance": 100000.0}},
    }
    out = check_margin_for_orders(
        facade, [{"a": 1}, {"b": 2}], fallback_required=8000.0,
    )
    assert out.ok
    assert out.required == 8000.0


def test_margin_fail_closed_when_usable_funds_absent():
    facade = MagicMock()
    facade.basket_order_margins.return_value = {"final": {"total": 1000.0}}
    facade.margins.return_value = {"equity": {"available": {}}}
    out = check_margin_for_orders(facade, [{"variety": "regular"}])
    assert not out.ok
    assert "did not report usable funds" in out.message
