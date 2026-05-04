"""
tests/test_providers/test_zerodha/test_instruments.py
=====================================================

Tests for the `InstrumentMaster` daily cache.
"""

from __future__ import annotations

from datetime import date

import pytest

from providers.zerodha.instruments import Instrument, InstrumentMaster


def _row(
    *,
    token: int = 100,
    tradingsymbol: str = "NIFTY26MAY23000CE",
    name: str = "NIFTY",
    expiry: str = "2026-05-28",
    strike: float = 23000.0,
    instrument_type: str = "CE",
    exchange: str = "NFO",
    segment: str = "NFO-OPT",
) -> dict:
    return {
        "instrument_token": token,
        "exchange_token": token + 1,
        "tradingsymbol": tradingsymbol,
        "name": name,
        "expiry": expiry,
        "strike": strike,
        "tick_size": 0.05,
        "lot_size": 25,
        "instrument_type": instrument_type,
        "segment": segment,
        "exchange": exchange,
    }


def _sample_rows():
    return [
        _row(token=1, tradingsymbol="NIFTY26MAY23000CE", strike=23000.0, instrument_type="CE"),
        _row(token=2, tradingsymbol="NIFTY26MAY23000PE", strike=23000.0, instrument_type="PE"),
        _row(token=3, tradingsymbol="NIFTY26MAY23100CE", strike=23100.0, instrument_type="CE"),
        _row(token=4, tradingsymbol="NIFTY26JUN23000CE", strike=23000.0, instrument_type="CE",
             expiry="2026-06-25"),
        _row(token=5, tradingsymbol="NIFTY 50", name="", expiry="", strike=0,
             instrument_type="EQ", exchange="NSE", segment="INDICES"),
    ]


def test_refresh_loads_all_rows():
    im = InstrumentMaster(loader=_sample_rows)
    n = im.refresh()
    assert n == 5
    assert im.size == 5
    assert im.loaded is True


def test_get_by_tradingsymbol():
    im = InstrumentMaster(loader=_sample_rows)
    im.refresh()
    inst = im.get_by_tradingsymbol("NFO", "NIFTY26MAY23000CE")
    assert inst is not None
    assert inst.instrument_token == 1
    assert inst.strike == 23000.0


def test_get_option_lookup():
    im = InstrumentMaster(loader=_sample_rows)
    im.refresh()
    inst = im.get_option("NIFTY", date(2026, 5, 28), 23000.0, "CE")
    assert inst is not None
    assert inst.instrument_token == 1


def test_get_option_returns_none_for_unknown():
    im = InstrumentMaster(loader=_sample_rows)
    im.refresh()
    assert im.get_option("NIFTY", date(2026, 5, 28), 99999.0, "CE") is None


def test_list_options_filters_by_expiry():
    im = InstrumentMaster(loader=_sample_rows)
    im.refresh()
    may = im.list_options("NIFTY", date(2026, 5, 28))
    assert len(may) == 3   # 23000CE, 23000PE, 23100CE
    jun = im.list_options("NIFTY", date(2026, 6, 25))
    assert len(jun) == 1


def test_list_expiries_sorted_unique():
    im = InstrumentMaster(loader=_sample_rows)
    im.refresh()
    out = im.list_expiries("NIFTY")
    assert out == [date(2026, 5, 28), date(2026, 6, 25)]


def test_refresh_if_stale_skips_when_fresh(monkeypatch):
    import time as _t
    fake = {"t": 1000.0}
    monkeypatch.setattr(_t, "monotonic", lambda: fake["t"])
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return _sample_rows()

    im = InstrumentMaster(loader=loader, ttl_seconds=3600)
    im.refresh_if_stale()
    assert calls["n"] == 1
    fake["t"] += 100
    im.refresh_if_stale()
    assert calls["n"] == 1   # still fresh, no refresh
    fake["t"] += 4000
    im.refresh_if_stale()
    assert calls["n"] == 2   # past TTL, refreshed


def test_malformed_row_skipped():
    im = InstrumentMaster(loader=lambda: [
        _row(token=1, tradingsymbol="OK"),
        {"garbage": "row"},
    ])
    n = im.refresh()
    assert n == 1


def test_invalid_ttl_raises():
    with pytest.raises(ValueError):
        InstrumentMaster(loader=lambda: [], ttl_seconds=0)
