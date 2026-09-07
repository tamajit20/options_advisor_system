"""Tests for providers/zerodha/leg_quotes.py (read-only live leg prices)."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from providers.zerodha import leg_quotes
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


def _leg(leg_order: int = 1) -> dict:
    return {
        "leg_order": leg_order,
        "symbol": "NIFTY",
        "expiry_date": date(2026, 5, 28),
        "strike": 23000.0,
        "option_type": "CE",
        "action": "BUY",
        "suggested_price": 100.0,
        "suggested_price_low": 95.0,
        "suggested_price_high": 105.0,
    }


@pytest.fixture(autouse=True)
def _clean_caches():
    leg_quotes.invalidate_caches()
    yield
    leg_quotes.invalidate_caches()


def test_no_legs_is_unavailable():
    out = leg_quotes.fetch_leg_live_prices([])
    assert out == {"available": False, "reason": "no_legs"}


def test_missing_session_is_unavailable():
    with patch.dict(leg_quotes.ZERODHA_API_CONFIG, {"api_key": "k"}, clear=False), \
         patch.object(leg_quotes, "load_session", return_value=None):
        out = leg_quotes.fetch_leg_live_prices([_leg()])
    assert out["available"] is False
    assert out["reason"] == "no_valid_session"


def _patched_kite(ltp_response):
    facade = MagicMock()
    facade.ltp.return_value = ltp_response
    master = MagicMock()
    master.get_option.return_value = _inst()
    return facade, master


def test_returns_ltp_and_band_flag():
    facade, master = _patched_kite({"NFO:NIFTY26MAY23000CE": {"last_price": 101.5}})
    with patch.dict(leg_quotes.ZERODHA_API_CONFIG, {"api_key": "k"}, clear=False), \
         patch.object(leg_quotes, "load_session", return_value=MagicMock(access_token="t")), \
         patch.object(leg_quotes, "is_token_valid", return_value=True), \
         patch.object(leg_quotes, "KiteFacade", return_value=facade), \
         patch.object(leg_quotes, "_get_master", return_value=master):
        out = leg_quotes.fetch_leg_live_prices([_leg()])

    assert out["available"] is True
    assert out["legs"][0]["ltp"] == 101.5
    assert out["legs"][0]["in_band"] is True
    assert out["legs"][0]["tradingsymbol"] == "NIFTY26MAY23000CE"


def test_out_of_band_ltp_is_flagged():
    facade, master = _patched_kite({"NFO:NIFTY26MAY23000CE": {"last_price": 400.0}})
    with patch.dict(leg_quotes.ZERODHA_API_CONFIG, {"api_key": "k"}, clear=False), \
         patch.object(leg_quotes, "load_session", return_value=MagicMock(access_token="t")), \
         patch.object(leg_quotes, "is_token_valid", return_value=True), \
         patch.object(leg_quotes, "KiteFacade", return_value=facade), \
         patch.object(leg_quotes, "_get_master", return_value=master):
        out = leg_quotes.fetch_leg_live_prices([_leg()])

    assert out["available"] is True
    assert out["legs"][0]["in_band"] is False


def test_second_call_is_served_from_quote_cache():
    facade, master = _patched_kite({"NFO:NIFTY26MAY23000CE": {"last_price": 101.5}})
    with patch.dict(leg_quotes.ZERODHA_API_CONFIG, {"api_key": "k"}, clear=False), \
         patch.object(leg_quotes, "load_session", return_value=MagicMock(access_token="t")), \
         patch.object(leg_quotes, "is_token_valid", return_value=True), \
         patch.object(leg_quotes, "KiteFacade", return_value=facade), \
         patch.object(leg_quotes, "_get_master", return_value=master):
        leg_quotes.fetch_leg_live_prices([_leg()])
        leg_quotes.fetch_leg_live_prices([_leg()])

    assert facade.ltp.call_count == 1


def test_broker_error_is_swallowed():
    facade = MagicMock()
    facade.ltp.side_effect = RuntimeError("kite down")
    master = MagicMock()
    master.get_option.return_value = _inst()
    with patch.dict(leg_quotes.ZERODHA_API_CONFIG, {"api_key": "k"}, clear=False), \
         patch.object(leg_quotes, "load_session", return_value=MagicMock(access_token="t")), \
         patch.object(leg_quotes, "is_token_valid", return_value=True), \
         patch.object(leg_quotes, "KiteFacade", return_value=facade), \
         patch.object(leg_quotes, "_get_master", return_value=master):
        out = leg_quotes.fetch_leg_live_prices([_leg()])

    assert out["available"] is False
    assert "kite down" in out["reason"]


def test_unresolvable_instrument_is_unavailable():
    facade = MagicMock()
    master = MagicMock()
    master.get_option.return_value = None
    with patch.dict(leg_quotes.ZERODHA_API_CONFIG, {"api_key": "k"}, clear=False), \
         patch.object(leg_quotes, "load_session", return_value=MagicMock(access_token="t")), \
         patch.object(leg_quotes, "is_token_valid", return_value=True), \
         patch.object(leg_quotes, "KiteFacade", return_value=facade), \
         patch.object(leg_quotes, "_get_master", return_value=master):
        out = leg_quotes.fetch_leg_live_prices([_leg()])

    assert out["available"] is False
    assert out["reason"] == "instruments_not_found"
