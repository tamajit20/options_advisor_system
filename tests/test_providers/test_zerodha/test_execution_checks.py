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
    assert out.peak_required == 20000.0
    assert out.final_required == 20000.0
    assert out.available == 25000.0


def test_margin_gates_on_path_peak_not_final():
    """Legs can spike mid-sequence above the finished-structure margin."""
    facade = MagicMock()

    def _basket(orders, **_kwargs):
        totals = {1: 100.0, 2: 300.0, 3: 250.0, 4: 100.0}
        return {"final": {"total": totals[len(orders)]}}

    facade.basket_order_margins.side_effect = _basket
    # Peak 300 + 5% = 315; final is only 100 — must still fail.
    facade.margins.return_value = {
        "equity": {"available": {"live_balance": 280.0}},
    }
    params = [
        {
            "variety": "regular", "exchange": "NFO", "tradingsymbol": f"LEG{i}",
            "transaction_type": "SELL" if i <= 2 else "BUY",
            "quantity": 50, "product": "NRML", "order_type": "LIMIT",
            "price": 10.0, "leg_order": i,
        }
        for i in range(1, 5)
    ]
    out = check_margin_for_orders(facade, params)
    assert not out.ok
    assert out.peak_required == 300.0
    assert out.final_required == 100.0
    assert out.required == 300.0
    assert len(out.path_steps) == 4
    assert out.path_steps[0]["delta"] == 100.0
    assert out.path_steps[1]["delta"] == 200.0
    assert out.path_steps[2]["delta"] == -50.0
    assert out.path_steps[3]["delta"] == -150.0
    assert "peak" in out.message.lower()


def test_margin_path_peak_ok_when_available_covers_peak():
    facade = MagicMock()

    def _basket(orders, **_kwargs):
        totals = {1: 100.0, 2: 300.0, 3: 250.0, 4: 100.0}
        return {"final": {"total": totals[len(orders)]}}

    facade.basket_order_margins.side_effect = _basket
    facade.margins.return_value = {
        "equity": {"available": {"live_balance": 400.0}},
    }
    params = [
        {
            "variety": "regular", "exchange": "NFO", "tradingsymbol": f"LEG{i}",
            "transaction_type": "BUY", "quantity": 50, "product": "NRML",
            "order_type": "LIMIT", "price": 10.0, "leg_order": i,
        }
        for i in range(1, 5)
    ]
    out = check_margin_for_orders(facade, params)
    assert out.ok
    assert out.peak_required == 300.0
    assert out.final_required == 100.0
    assert facade.basket_order_margins.call_count == 4


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
    assert "did not report available margin" in out.message


def test_reducing_positions_blocks_exit_when_flat():
    from providers.zerodha.execution_checks import assert_reducing_positions_exist

    facade = MagicMock()
    facade.positions.return_value = {"net": []}
    legs = [{"leg_order": 1, "action": "SELL", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = assert_reducing_positions_exist(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert not out.ok
    assert "no matching open position" in out.message


def test_reducing_positions_allows_sell_when_long():
    from providers.zerodha.execution_checks import assert_reducing_positions_exist

    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": 50}],
    }
    legs = [{"leg_order": 1, "action": "SELL", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = assert_reducing_positions_exist(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert out.ok


def test_reducing_positions_allows_buy_when_short():
    from providers.zerodha.execution_checks import assert_reducing_positions_exist

    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": -50}],
    }
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = assert_reducing_positions_exist(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert out.ok


def test_reducing_positions_fail_closed_on_positions_error():
    from providers.zerodha.execution_checks import assert_reducing_positions_exist

    facade = MagicMock()
    facade.positions.side_effect = RuntimeError("kite down")
    legs = [{"leg_order": 1, "action": "SELL", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = assert_reducing_positions_exist(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert not out.ok
    assert "Could not fetch Zerodha positions" in out.message


def test_reducing_positions_ignores_disable_flag(monkeypatch):
    from providers.zerodha import execution_checks as ec

    monkeypatch.setitem(ec.ZERODHA_EXECUTION_CONFIG, "exit_position_check_enabled", False)
    facade = MagicMock()
    facade.positions.return_value = {"net": []}
    legs = [{"leg_order": 1, "action": "SELL", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = ec.assert_reducing_positions_exist(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert not out.ok


def test_exposure_fail_closed_on_positions_error():
    facade = MagicMock()
    facade.positions.side_effect = RuntimeError("kite down")
    legs = [{"leg_order": 1, "action": "BUY", "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = check_exposure_conflicts(
        facade, legs, inst_map,
        transaction_fn=lambda leg: leg["action"],
    )
    assert not out.ok
    assert "could not fetch zerodha positions" in out.message.lower()


def test_exit_reopen_refuses_when_exit_not_on_broker():
    from providers.zerodha.execution_checks import assert_exit_fill_reflected_for_reopen

    facade = MagicMock()
    # Still fully long — EXIT SELL never happened on Kite
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": 50}],
    }
    legs = [{"leg_order": 1, "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = assert_exit_fill_reflected_for_reopen(
        facade, legs, inst_map,
        close_transaction_fn=lambda _: "SELL",
    )
    assert not out.ok
    assert "still long" in out.message


def test_exit_reopen_allows_when_flat_after_exit():
    from providers.zerodha.execution_checks import assert_exit_fill_reflected_for_reopen

    facade = MagicMock()
    facade.positions.return_value = {"net": []}
    legs = [{"leg_order": 1, "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    out = assert_exit_fill_reflected_for_reopen(
        facade, legs, inst_map,
        close_transaction_fn=lambda _: "SELL",
    )
    assert out.ok


def test_is_structure_flat_on_broker():
    from providers.zerodha.execution_checks import is_structure_flat_on_broker

    facade = MagicMock()
    facade.positions.return_value = {"net": []}
    legs = [{"leg_order": 1}, {"leg_order": 2}]
    inst_map = {
        1: _inst("NIFTY26MAY23000CE"),
        2: _inst("NIFTY26MAY23100PE"),
    }
    assert is_structure_flat_on_broker(facade, legs, inst_map) is True

    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26MAY23000CE", "quantity": 50}],
    }
    assert is_structure_flat_on_broker(facade, legs, inst_map) is False


def test_find_external_exit_fills_matches_complete_order():
    from providers.zerodha.execution_checks import find_external_exit_fills

    facade = MagicMock()
    facade.orders.return_value = [
        {
            "order_id": "O1",
            "status": "COMPLETE",
            "tradingsymbol": "NIFTY26MAY23000CE",
            "transaction_type": "SELL",
            "average_price": 42.5,
            "filled_quantity": 50,
            "quantity": 50,
            "order_timestamp": "2026-05-12 14:00:00",
        },
    ]
    legs = [{"leg_order": 1, "lots": 1, "lot_size": 50}]
    inst_map = {1: _inst("NIFTY26MAY23000CE")}
    found, missing = find_external_exit_fills(
        facade, legs, inst_map,
        transaction_fn=lambda _: "SELL",
    )
    assert not missing
    assert found[1]["fill_price"] == 42.5
    assert found[1]["kite_order_id"] == "O1"