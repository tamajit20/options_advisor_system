"""Tests for providers/zerodha/order_pricing.py"""

from providers.zerodha.instruments import Instrument
from providers.zerodha.order_pricing import (
    limit_from_reference,
    profile_for,
    reference_price,
)
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


def test_reference_price_uses_ask_for_buy():
    quote = {"depth": {"sell": [{"price": 102.0}], "buy": [{"price": 98.0}]}}
    px = reference_price(ltp=100.0, quote_row=quote, transaction_type="BUY", use_bid_ask=True)
    assert px == 102.0


def test_limit_walks_on_retry():
    inst = _inst()
    p0 = limit_from_reference(100.0, "BUY", inst, slippage_pct=0.5, attempt=0)
    p1 = limit_from_reference(100.0, "BUY", inst, slippage_pct=0.5, attempt=2, slip_walk_per_retry=0.25)
    assert p1 > p0


def test_rollback_profile_wider_slip():
    rb = profile_for("rollback")
    entry = profile_for("entry")
    assert rb.slippage_pct >= entry.slippage_pct
