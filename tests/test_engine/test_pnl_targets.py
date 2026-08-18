"""Tests for engine.pnl_targets — shared Exit Plan / TAKE_PROFIT / TARGET_HIT."""
from __future__ import annotations

import math

import pytest

from engine.pnl_targets import (
    credit_capture_fraction,
    debit_spread_target_fraction,
    is_debit_spread_strategy,
    is_long_premium_strategy,
    long_premium_target_multiple,
    pnl_rules_public,
    profit_target_trade_rs,
    take_profit_hit,
)


def test_seven_dte_is_one_times_debit():
    assert long_premium_target_multiple(7) == pytest.approx(1.0)


def test_calendar_is_long_premium():
    assert is_long_premium_strategy("CALENDAR_SPREAD")
    assert not is_debit_spread_strategy("CALENDAR_SPREAD")


def test_bear_put_is_debit_spread():
    assert is_debit_spread_strategy("BEAR_PUT_SPREAD")
    assert not is_long_premium_strategy("BEAR_PUT_SPREAD")


def test_iron_butterfly_uses_config_capture():
    assert credit_capture_fraction("IRON_BUTTERFLY") == pytest.approx(0.75)
    assert credit_capture_fraction("IRON_CONDOR") == pytest.approx(0.50)


def test_calendar_target_is_debit_multiple_not_max_profit():
    # 7 DTE, debit ₹10,500 → target ₹10,500 (100%), not 80% of max_profit.
    target, kind, mult = profit_target_trade_rs(
        strategy="CALENDAR_SPREAD",
        dte=7,
        max_profit_rs=8000.0,
        entry_net_credit=-10500.0,
    )
    assert kind == "long_premium"
    assert mult == pytest.approx(1.0)
    assert target == pytest.approx(10500.0)


def test_bear_put_target_is_half_debit():
    target, kind, frac = profit_target_trade_rs(
        strategy="BEAR_PUT_SPREAD",
        dte=10,
        max_profit_rs=17036.25,
        entry_net_credit=-8775.0,
    )
    assert kind == "debit_spread"
    assert frac == pytest.approx(debit_spread_target_fraction())
    assert target == pytest.approx(4387.5)


def test_iron_condor_target_is_half_max_profit():
    target, kind, frac = profit_target_trade_rs(
        strategy="IRON_CONDOR",
        dte=10,
        max_profit_rs=10000.0,
        entry_net_credit=10000.0,
    )
    assert kind == "credit"
    assert frac == pytest.approx(0.50)
    assert target == pytest.approx(5000.0)


def test_take_profit_hit_long_premium_ignores_infinite_max_profit():
    hit, reason = take_profit_hit(
        strategy="LONG_STRADDLE",
        dte=7,
        current_pnl=12000.0,
        max_profit_rs=math.inf,
        entry_net_credit=-10000.0,
    )
    assert hit is True
    assert "100%" in reason
    assert "debit" in reason.lower()


def test_take_profit_not_hit_below_debit_target():
    hit, _ = take_profit_hit(
        strategy="CALENDAR_SPREAD",
        dte=7,
        current_pnl=5000.0,
        max_profit_rs=8000.0,
        entry_net_credit=-10500.0,
    )
    assert hit is False


def test_pnl_rules_public_has_dashboard_keys():
    rules = pnl_rules_public()
    assert "strategy_sl_limits" in rules
    assert "LONG_STRADDLE" in rules["strategy_sl_limits"]
    assert rules["long_premium_target_base"] == pytest.approx(0.50)
    assert "CALENDAR_SPREAD" in rules["long_premium_target_strategies"]
    assert "BEAR_PUT_SPREAD" in rules["debit_spread_target_strategies"]
    assert rules["strategy_take_profit_fraction"]["IRON_BUTTERFLY"] == pytest.approx(0.75)
