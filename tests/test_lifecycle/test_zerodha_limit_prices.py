"""Tests for optional user limit prices in zerodha_executor."""

from lifecycle.zerodha_executor import _resolve_limit_price, parse_leg_limits
from providers.zerodha.instruments import Instrument
from datetime import date


def _inst():
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


def test_resolve_limit_uses_user_price():
    px = _resolve_limit_price(100.0, "BUY", _inst(), user_limit=102.3)
    assert px == 102.3


def test_resolve_limit_auto_buy_slippage():
    px = _resolve_limit_price(100.0, "BUY", _inst(), user_limit=None)
    assert px == 100.5  # 0.5% default slippage


def test_parse_leg_limits_list():
    assert parse_leg_limits([
        {"leg_order": 1, "limit_price": 10.5},
        {"leg_order": 2, "limit_price": 20.0},
    ]) == {1: 10.5, 2: 20.0}


def test_parse_leg_limits_skips_empty():
    assert parse_leg_limits([]) == {}
    assert parse_leg_limits(None) == {}
