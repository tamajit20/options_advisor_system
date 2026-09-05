"""Parametric leg-order and side tests for every supported strategy."""

from datetime import date

import pytest

from lifecycle.leg_execution_order import legs_in_execution_order
from lifecycle.zerodha_executor import _transaction_type_for_leg


_EXP = date(2026, 5, 28)


def _leg(lo: int, action: str, opt: str, strike: float) -> dict:
    return {
        "leg_order": lo,
        "action": action,
        "option_type": opt,
        "symbol": "NIFTY",
        "expiry_date": _EXP,
        "strike": strike,
        "lots": 1,
        "lot_size": 50,
    }


# Canonical leg shapes mirroring engine/leg_builder.py
_STRATEGY_LEGS = {
    "IRON_CONDOR": [
        _leg(1, "SELL", "PE", 22800),
        _leg(2, "BUY", "PE", 22700),
        _leg(3, "SELL", "CE", 23200),
        _leg(4, "BUY", "CE", 23300),
    ],
    "IRON_BUTTERFLY": [
        _leg(1, "SELL", "PE", 23000),
        _leg(2, "BUY", "PE", 22800),
        _leg(3, "SELL", "CE", 23000),
        _leg(4, "BUY", "CE", 23200),
    ],
    "BULL_PUT_SPREAD": [
        _leg(1, "SELL", "PE", 22800),
        _leg(2, "BUY", "PE", 22700),
    ],
    "BEAR_CALL_SPREAD": [
        _leg(1, "SELL", "CE", 23200),
        _leg(2, "BUY", "CE", 23300),
    ],
    "BULL_CALL_SPREAD": [
        _leg(1, "BUY", "CE", 23000),
        _leg(2, "SELL", "CE", 23100),
    ],
    "BEAR_PUT_SPREAD": [
        _leg(1, "BUY", "PE", 23000),
        _leg(2, "SELL", "PE", 22900),
    ],
    "LONG_STRADDLE": [
        _leg(1, "BUY", "CE", 23000),
        _leg(2, "BUY", "PE", 23000),
    ],
    "LONG_STRANGLE": [
        _leg(1, "BUY", "CE", 23200),
        _leg(2, "BUY", "PE", 22800),
    ],
    "LONG_CALL": [
        _leg(1, "BUY", "CE", 23000),
    ],
    "LONG_PUT": [
        _leg(1, "BUY", "PE", 23000),
    ],
    "JADE_LIZARD": [
        _leg(1, "SELL", "PE", 22800),
        _leg(2, "SELL", "CE", 23100),
        _leg(3, "BUY", "CE", 23200),
    ],
    "CALENDAR_SPREAD": [
        _leg(1, "SELL", "CE", 23000),
        _leg(2, "BUY", "CE", 23000),
    ],
}


@pytest.mark.parametrize("strategy,legs", list(_STRATEGY_LEGS.items()))
def test_entry_buys_before_sells(strategy, legs):
    if len(legs) <= 1:
        return
    ordered = legs_in_execution_order(legs, strategy, mode="entry")
    seen_sell = False
    for leg in ordered:
        act = leg["action"].upper()
        if act == "SELL":
            seen_sell = True
        elif act == "BUY" and seen_sell:
            pytest.fail(f"{strategy}: BUY leg {leg['leg_order']} after a SELL")


@pytest.mark.parametrize("strategy,legs", list(_STRATEGY_LEGS.items()))
def test_close_sells_before_buys(strategy, legs):
    if len(legs) <= 1:
        return
    ordered = legs_in_execution_order(legs, strategy, mode="close")
    seen_buy = False
    for leg in ordered:
        act = leg["action"].upper()
        if act == "BUY":
            seen_buy = True
        elif act == "SELL" and seen_buy:
            pytest.fail(f"{strategy}: original SELL leg {leg['leg_order']} after original BUY on close")


@pytest.mark.parametrize("strategy,legs", list(_STRATEGY_LEGS.items()))
def test_entry_kite_side_matches_leg_action(strategy, legs):
    for leg in legs:
        txn = _transaction_type_for_leg(leg, "entry")
        assert txn == leg["action"].upper()


@pytest.mark.parametrize("strategy,legs", list(_STRATEGY_LEGS.items()))
def test_close_kite_side_flips_entry_action(strategy, legs):
    for leg in legs:
        entry = leg["action"].upper()
        close = _transaction_type_for_leg(leg, "close")
        assert close != entry
        assert close in ("BUY", "SELL")


def test_jade_lizard_entry_sequence_exact():
    legs = _STRATEGY_LEGS["JADE_LIZARD"]
    ordered = legs_in_execution_order(legs, "JADE_LIZARD", mode="entry")
    assert [l["leg_order"] for l in ordered] == [3, 2, 1]


def test_jade_lizard_close_sequence_exact():
    legs = _STRATEGY_LEGS["JADE_LIZARD"]
    ordered = legs_in_execution_order(legs, "JADE_LIZARD", mode="close")
    assert [l["leg_order"] for l in ordered] == [1, 2, 3]
