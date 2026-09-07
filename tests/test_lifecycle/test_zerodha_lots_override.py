"""Tests for the shared order-size (lots) override on the Zerodha entry path."""

from datetime import date

import pytest

from lifecycle.zerodha_executor import (
    ZerodhaExecutionError,
    _build_leg_plans,
    _suggested_lots,
    _with_lots_override,
    parse_lots_override,
)
from providers.zerodha.instruments import Instrument


def _inst(sym: str = "NIFTY26MAY23000CE") -> Instrument:
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


def _leg(lots: int = 1) -> dict:
    return {
        "leg_order": 1,
        "action": "BUY",
        "option_type": "CE",
        "strike": 23000.0,
        "lots": lots,
        "lot_size": 50,
        "suggested_price": 100.0,
        "suggested_price_low": 95.0,
        "suggested_price_high": 105.0,
    }


def test_parse_lots_override_accepts_positive_int():
    assert parse_lots_override(2) == 2
    assert parse_lots_override("3") == 3


def test_parse_lots_override_blank_is_none():
    assert parse_lots_override(None) is None
    assert parse_lots_override("") is None


def test_parse_lots_override_rejects_zero_and_negative():
    for bad in (0, -1, "-4"):
        with pytest.raises(ZerodhaExecutionError, match="at least 1"):
            parse_lots_override(bad)


def test_parse_lots_override_rejects_non_numeric():
    with pytest.raises(ZerodhaExecutionError, match="Invalid lots"):
        parse_lots_override("abc")


def test_parse_lots_override_enforces_cap():
    from config import STRATEGY_CONFIG

    cap = int(STRATEGY_CONFIG.get("max_lots_cap") or 0)
    if cap <= 0:
        pytest.skip("max_lots_cap not configured")
    with pytest.raises(ZerodhaExecutionError, match="max_lots_cap"):
        parse_lots_override(cap + 1)


def test_with_lots_override_pins_lots_actual_without_mutating_source():
    legs = [_leg(lots=1)]
    out = _with_lots_override(legs, 3)
    assert out[0]["lots_actual"] == 3
    assert "lots_actual" not in legs[0]


def test_with_lots_override_noop_when_none():
    legs = [_leg(lots=2)]
    assert _with_lots_override(legs, None) is legs


def test_suggested_lots_reads_first_positive_value():
    assert _suggested_lots([{"lots": 0}, {"lots": 2}]) == 2
    assert _suggested_lots([{}]) == 1


def test_leg_plan_quantity_follows_lots_override():
    legs = _with_lots_override([_leg(lots=1)], 3)
    plans = _build_leg_plans(
        legs, {1: _inst()}, {1: 100.0}, None, mode="entry", strategy="LONG_CALL",
    )
    assert plans[0].lots == 3
    assert plans[0].quantity == 150
