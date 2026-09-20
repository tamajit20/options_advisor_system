"""Tests for providers/zerodha/execution_facade.py"""

from unittest.mock import MagicMock

import pytest

from providers.zerodha.execution_facade import (
    KiteExecutionFacade,
    order_rate_cap,
    reset_order_rate_cap_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_order_cap():
    reset_order_rate_cap_for_tests()
    yield
    reset_order_rate_cap_for_tests()


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


def test_profile_proxies(facade, kite_mock):
    kite_mock.profile.return_value = {"user_id": "AB1234", "user_name": "Test"}
    out = facade.profile()
    assert out["user_id"] == "AB1234"
    kite_mock.profile.assert_called_once()


def test_margins_proxies(facade, kite_mock):
    kite_mock.margins.return_value = {"equity": {"net": 1000}}
    out = facade.margins()
    assert out["equity"]["net"] == 1000
    kite_mock.margins.assert_called_once_with()

    facade.margins("equity")
    kite_mock.margins.assert_called_with(segment="equity")


def test_place_modify_cancel_share_order_rate_cap(facade, kite_mock, mocker):
    mocker.patch(
        "providers.zerodha.execution_facade._configured_orders_per_sec",
        return_value=2,
    )
    reset_order_rate_cap_for_tests()
    kite_mock.place_order.return_value = "1"
    kite_mock.modify_order.return_value = "1"

    facade.place_order(
        variety="regular", exchange="NFO", tradingsymbol="X",
        transaction_type="BUY", quantity=1, product="NRML", order_type="MARKET",
    )
    facade.modify_order(order_id="1", variety="regular", price=1.0)
    assert order_rate_cap().used == 2
    assert order_rate_cap().try_acquire() is False


def test_place_order_waits_when_cap_full(facade, kite_mock, mocker):
    mocker.patch(
        "providers.zerodha.execution_facade._configured_orders_per_sec",
        return_value=1,
    )
    reset_order_rate_cap_for_tests()
    kite_mock.place_order.return_value = "OID"

    fake = {"t": 100.0}
    sleeps = []
    mocker.patch(
        "providers.zerodha.rate_limiter.time.monotonic",
        side_effect=lambda: fake["t"],
    )

    def _sleep(d):
        sleeps.append(d)
        fake["t"] += d

    mocker.patch("providers.zerodha.rate_limiter.time.sleep", side_effect=_sleep)

    facade.place_order(
        variety="regular", exchange="NFO", tradingsymbol="X",
        transaction_type="BUY", quantity=1, product="NRML", order_type="MARKET",
    )
    facade.place_order(
        variety="regular", exchange="NFO", tradingsymbol="Y",
        transaction_type="BUY", quantity=1, product="NRML", order_type="MARKET",
    )
    assert kite_mock.place_order.call_count == 2
    assert sum(sleeps) == pytest.approx(1.0, abs=1e-6)
