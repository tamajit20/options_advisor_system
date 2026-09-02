"""Tests for providers/zerodha/execution_facade.py"""

from unittest.mock import MagicMock

import pytest

from providers.zerodha.execution_facade import KiteExecutionFacade


@pytest.fixture
def kite_mock():
    return MagicMock(name="kite_client")


@pytest.fixture
def facade(kite_mock):
    return KiteExecutionFacade("key", "tok", kite_client=kite_mock)


def test_place_order_proxies(facade, kite_mock):
    kite_mock.place_order.return_value = "12345"
    out = facade.place_order(
        variety="regular",
        exchange="NFO",
        tradingsymbol="NIFTY26MAY23000CE",
        transaction_type="BUY",
        quantity=50,
        product="NRML",
        order_type="LIMIT",
        price=10.5,
    )
    assert out == "12345"
    kite_mock.place_order.assert_called_once()


def test_order_history_proxies(facade, kite_mock):
    kite_mock.order_history.return_value = [{"status": "COMPLETE"}]
    assert facade.order_history("12345")[0]["status"] == "COMPLETE"
