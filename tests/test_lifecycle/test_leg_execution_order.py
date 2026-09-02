"""Tests for lifecycle/leg_execution_order.py"""

from lifecycle.leg_execution_order import leg_execution_order, legs_in_execution_order


def test_entry_buys_before_sells():
    legs = [
        {"leg_order": 1, "action": "SELL", "option_type": "CE"},
        {"leg_order": 2, "action": "BUY", "option_type": "CE"},
    ]
    order = leg_execution_order(legs, "BULL_PUT_SPREAD", mode="entry")
    assert order[2] == 1
    assert order[1] == 2


def test_close_sells_before_buys():
    legs = [
        {"leg_order": 1, "action": "SELL", "option_type": "CE"},
        {"leg_order": 2, "action": "BUY", "option_type": "CE"},
    ]
    order = leg_execution_order(legs, "BULL_PUT_SPREAD", mode="close")
    assert order[1] == 1
    assert order[2] == 2


def test_single_leg_returns_empty_order():
    legs = [{"leg_order": 1, "action": "BUY", "option_type": "CE"}]
    assert leg_execution_order(legs, "LONG_CALL", mode="entry") == {}
    assert legs_in_execution_order(legs, "LONG_CALL") == legs
