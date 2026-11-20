"""Shared instrument master: one Kite download per token/scope until TTL."""

from __future__ import annotations

from datetime import date

import pytest

from providers.zerodha import instrument_cache as ic


def _rows():
    return [
        {
            "instrument_token": 1,
            "exchange_token": 2,
            "tradingsymbol": "NIFTY26MAY23000CE",
            "name": "NIFTY",
            "expiry": "2026-05-28",
            "strike": 23000.0,
            "tick_size": 0.05,
            "lot_size": 25,
            "instrument_type": "CE",
            "segment": "NFO-OPT",
            "exchange": "NFO",
        },
    ]


class _FakeFacade:
    def __init__(self):
        self.calls = 0
        self.access_token = "tok-A"

    def instruments(self, segment=None):
        self.calls += 1
        return _rows()


@pytest.fixture(autouse=True)
def _clear_cache():
    ic.invalidate_shared_instrument_master()
    yield
    ic.invalidate_shared_instrument_master()


def test_shared_master_downloads_once_per_token(monkeypatch):
    fake = {"t": 1000.0}
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: fake["t"])

    facade = _FakeFacade()
    m1 = ic.get_shared_instrument_master(
        api_key="k", access_token="tok-A", segment="NFO", facade=facade,
    )
    m2 = ic.get_shared_instrument_master(
        api_key="k", access_token="tok-A", segment="NFO", facade=facade,
    )
    assert m1 is m2
    assert facade.calls == 1
    inst = m1.get_option("NIFTY", date(2026, 5, 28), 23000.0, "CE")
    assert inst is not None
    assert inst.instrument_token == 1


def test_token_change_reloads_master():
    facade = _FakeFacade()
    ic.get_shared_instrument_master(
        api_key="k", access_token="tok-A", segment="NFO", facade=facade,
    )
    facade.access_token = "tok-B"
    ic.get_shared_instrument_master(
        api_key="k", access_token="tok-B", segment="NFO", facade=facade,
    )
    assert facade.calls == 2
    assert ic.shared_instrument_master_stats()["entries"] == 1  # old token dropped


def test_invalidate_clears_cache():
    facade = _FakeFacade()
    ic.get_shared_instrument_master(
        api_key="k", access_token="tok-A", segment="NFO", facade=facade,
    )
    ic.invalidate_shared_instrument_master()
    assert ic.shared_instrument_master_stats()["entries"] == 0
    ic.get_shared_instrument_master(
        api_key="k", access_token="tok-A", segment="NFO", facade=facade,
    )
    assert facade.calls == 2
