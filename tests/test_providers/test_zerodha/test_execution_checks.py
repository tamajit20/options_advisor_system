"""Tests for providers/zerodha/execution_checks.py"""

from unittest.mock import MagicMock

import pytest

from providers.zerodha.execution_checks import (
    _required_from_margin_response,
    build_order_margin_params,
    check_exposure_conflicts,
    check_margin_for_orders,
    hedged_sell_block_reason,
    wait_until_hedged_sell_fits,
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
        "equity": {"net": 5000.0, "available": {"live_balance": 5000.0}},
    }
    out = check_margin_for_orders(facade, [{"variety": "regular"}])
    assert not out.ok
    assert out.required == 20000.0
    assert out.available == 5000.0
    assert out.shortfall > 0
    assert "Add ₹" in out.message or "Add" in out.message
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


def test_margin_preview_uses_hedged_final_not_naked_initial():
    """Place stays allowed when the finished spread fits. The naked sell bill is a race, not this gate."""
    facade = MagicMock()

    def _basket(orders, **_kwargs):
        if len(orders) == 1:
            return {
                "initial": {"total": 23270.0, "option_premium": 23270.0},
                "final": {"total": 23270.0, "option_premium": 23270.0},
            }
        return {
            "initial": {"total": 287849.86, "option_premium": 23270.0},
            "final": {"total": 41550.89, "option_premium": 11245.0},
        }

    facade.basket_order_margins.side_effect = _basket
    facade.margins.return_value = {
        "equity": {
            "net": 278704.60,
            "available": {"live_balance": 44738.17, "cash": 44738.17},
        },
    }
    params = [
        {
            "variety": "regular", "exchange": "NFO", "tradingsymbol": "NIFTY26NOV23300CE",
            "transaction_type": "BUY", "quantity": 65, "product": "NRML",
            "order_type": "LIMIT", "price": 358.0, "leg_order": 1,
        },
        {
            "variety": "regular", "exchange": "NFO", "tradingsymbol": "NIFTY26NOV23650CE",
            "transaction_type": "SELL", "quantity": 65, "product": "NRML",
            "order_type": "LIMIT", "price": 185.0, "leg_order": 2,
        },
    ]
    out = check_margin_for_orders(facade, params)
    assert out.ok
    assert out.peak_required == 41550.89
    assert out.final_required == 41550.89
    # Gate uses equity net (Kite "Margin available"), not cash alone.
    assert out.available == 278704.60
    assert out.shortfall == 0.0


def test_hedged_sell_waits_until_margin_picture_includes_the_long():
    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26NOV23300CE", "quantity": 65}],
    }
    facade.basket_order_margins.side_effect = [
        {"initial": {"total": 287849.86}, "final": {"total": 287849.86}},
        {"initial": {"total": 18280.0}, "final": {"total": 18280.0}},
    ]
    facade.margins.return_value = {
        "equity": {"net": 278704.60, "available": {"live_balance": 44738.17}},
    }
    sell = {
        "variety": "regular", "exchange": "NFO",
        "tradingsymbol": "NIFTY26NOV23650CE", "transaction_type": "SELL",
        "quantity": 65, "product": "NRML", "order_type": "LIMIT", "price": 185.0,
    }
    clock = {"t": 0.0}

    def monotonic():
        return clock["t"]

    def sleep(_sec):
        clock["t"] += 1.0

    reason = wait_until_hedged_sell_fits(
        facade, sell, {"NIFTY26NOV23300CE": 65},
        wait_sec=5, poll_sec=1, sleep_fn=sleep, monotonic_fn=monotonic,
    )
    assert reason is None
    assert facade.basket_order_margins.call_count == 2


def test_hedged_sell_reports_naked_bill_when_the_long_never_appears():
    facade = MagicMock()
    facade.positions.return_value = {
        "net": [{"tradingsymbol": "NIFTY26NOV23300CE", "quantity": 65}],
    }
    facade.basket_order_margins.return_value = {
        "initial": {"total": 287849.86}, "final": {"total": 287849.86},
    }
    facade.margins.return_value = {
        "equity": {"net": 278704.60, "available": {"live_balance": 44738.17}},
    }
    sell = {"variety": "regular", "tradingsymbol": "NIFTY26NOV23650CE", "transaction_type": "SELL"}
    reason = hedged_sell_block_reason(facade, sell, {"NIFTY26NOV23300CE": 65})
    assert reason is not None
    assert "287,849.86" in reason
    assert "278,704.60" in reason


def test_margin_blocks_when_cash_cannot_pay_option_premium():
    facade = MagicMock()
    facade.basket_order_margins.return_value = {
        "initial": {"total": 40000.0, "option_premium": 40000.0},
        "final": {"total": 40000.0, "option_premium": 40000.0},
    }
    facade.margins.return_value = {
        "equity": {
            "net": 300000.0,
            "available": {"live_balance": 5000.0},
        },
    }
    out = check_margin_for_orders(facade, [{"variety": "regular"}])
    assert not out.ok
    assert out.peak_required == 40000.0
    # available is equity net (SPAN gate); cash shortfall is in the message.
    assert out.available == 300000.0
    assert out.shortfall > 0
    assert "Option premium" in out.message
    assert "Add ₹" in out.message


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