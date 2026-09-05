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


def test_jade_lizard_entry_order():
    legs = [
        {"leg_order": 1, "action": "SELL", "option_type": "PE"},
        {"leg_order": 2, "action": "SELL", "option_type": "CE"},
        {"leg_order": 3, "action": "BUY", "option_type": "CE"},
    ]
    order = leg_execution_order(legs, "JADE_LIZARD", mode="entry")
    assert order[3] == 1
    assert order[2] == 2
    assert order[1] == 3


def test_jade_lizard_close_order():
    legs = [
        {"leg_order": 1, "action": "SELL", "option_type": "PE"},
        {"leg_order": 2, "action": "SELL", "option_type": "CE"},
        {"leg_order": 3, "action": "BUY", "option_type": "CE"},
    ]
    order = leg_execution_order(legs, "JADE_LIZARD", mode="close")
    assert order[1] == 1
    assert order[2] == 2
    assert order[3] == 3


def test_iron_condor_entry_buys_all_hedges_first():
    legs = [
        {"leg_order": 1, "action": "SELL", "option_type": "PE"},
        {"leg_order": 2, "action": "BUY", "option_type": "PE"},
        {"leg_order": 3, "action": "SELL", "option_type": "CE"},
        {"leg_order": 4, "action": "BUY", "option_type": "CE"},
    ]
    ordered = legs_in_execution_order(legs, "IRON_CONDOR", mode="entry")
    actions = [l["action"] for l in ordered]
    assert actions[:2] == ["BUY", "BUY"]
    assert actions[2:] == ["SELL", "SELL"]


def test_single_leg_returns_empty_order():
    legs = [{"leg_order": 1, "action": "BUY", "option_type": "CE"}]
    assert leg_execution_order(legs, "LONG_CALL", mode="entry") == {}
    assert legs_in_execution_order(legs, "LONG_CALL") == legs
