"""
tests/test_providers/test_zerodha/test_facade.py
================================================

Tests for the minimal `KiteFacade`. The facade exposes only three methods —
`set_access_token`, `instruments`, `ltp`. Anything else (orders, positions,
holdings, margins, GTTs, mutual funds, alerts) does not exist as an
attribute and accessing it raises `AttributeError`.

We never import `kiteconnect` here — every test injects a mock client via
`kite_client=`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from providers.zerodha.facade import KiteFacade


@pytest.fixture
def kite_mock():
    """A MagicMock standing in for `kiteconnect.KiteConnect`."""
    return MagicMock(name="kite_client")


@pytest.fixture
def facade(kite_mock):
    return KiteFacade(api_key="test_key", kite_client=kite_mock)


# ---- construction --------------------------------------------------------

def test_requires_api_key(kite_mock):
    with pytest.raises(ValueError):
        KiteFacade(api_key="", kite_client=kite_mock)


def test_sets_access_token_on_construction(kite_mock):
    KiteFacade(api_key="k", access_token="tok", kite_client=kite_mock)
    kite_mock.set_access_token.assert_called_once_with("tok")


def test_no_set_token_when_none(kite_mock):
    KiteFacade(api_key="k", kite_client=kite_mock)
    kite_mock.set_access_token.assert_not_called()


def test_api_key_property(facade):
    assert facade.api_key == "test_key"


# ---- the three public methods --------------------------------------------

def test_set_access_token_proxies(facade, kite_mock):
    facade.set_access_token("new_token")
    kite_mock.set_access_token.assert_called_with("new_token")


def test_ltp_proxies(facade, kite_mock):
    kite_mock.ltp.return_value = {"NSE:NIFTY 50": {"last_price": 23000.0}}
    out = facade.ltp(["NSE:NIFTY 50"])
    assert out == {"NSE:NIFTY 50": {"last_price": 23000.0}}
    kite_mock.ltp.assert_called_once_with(["NSE:NIFTY 50"])


def test_ltp_accepts_iterable(facade, kite_mock):
    kite_mock.ltp.return_value = {}
    facade.ltp(iter(["NSE:NIFTY 50", "NSE:INDIA VIX"]))
    kite_mock.ltp.assert_called_once_with(["NSE:NIFTY 50", "NSE:INDIA VIX"])


def test_instruments_no_arg(facade, kite_mock):
    kite_mock.instruments.return_value = [{"tradingsymbol": "NIFTY", "exchange": "NFO"}]
    out = facade.instruments()
    assert out == [{"tradingsymbol": "NIFTY", "exchange": "NFO"}]
    kite_mock.instruments.assert_called_once_with()


def test_instruments_with_exchange(facade, kite_mock):
    kite_mock.instruments.return_value = []
    facade.instruments("NFO")
    kite_mock.instruments.assert_called_once_with("NFO")


# ---- everything else does not exist --------------------------------------

@pytest.mark.parametrize("method", [
    # Order writes
    "place_order", "modify_order", "cancel_order", "exit_order",
    # Order reads
    "orders", "order_history", "trades",
    # Positions / portfolio
    "positions", "holdings", "convert_position",
    # Funds / margins
    "margins", "order_margins", "basket_order_margins",
    # Mutual funds
    "place_mf_order", "mf_holdings", "mf_sips", "place_mf_sip",
    # GTT / alerts
    "place_gtt", "modify_gtt", "delete_gtt", "gtts",
    "place_alert", "delete_alert",
    # Profile / misc
    "profile", "auctions",
])
def test_non_market_data_methods_do_not_exist(facade, method):
    """The facade must not expose any non-market-data Kite SDK method."""
    assert not hasattr(facade, method)
    with pytest.raises(AttributeError):
        getattr(facade, method)


def test_underlying_kite_client_not_exposed(facade):
    """The wrapped `KiteConnect` instance must not be reachable through any
    public attribute — otherwise callers could escape the facade."""
    public_attrs = [a for a in dir(facade) if not a.startswith("_")]
    # Whitelist the entire surface — anything new must be added explicitly.
    assert set(public_attrs) == {"api_key", "instruments", "ltp", "quote", "set_access_token"}


def test_place_order_call_never_reaches_underlying(facade, kite_mock):
    """Even if someone tries calling place_order, the underlying SDK is
    never invoked."""
    with pytest.raises(AttributeError):
        facade.place_order(  # type: ignore[attr-defined]
            variety="regular",
            tradingsymbol="NIFTY26MAY23000CE",
            exchange="NFO",
        )
    assert not kite_mock.place_order.called
