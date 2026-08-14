"""Tests for arb/instruments.py pairing logic."""

from __future__ import annotations

from providers.zerodha.instruments import Instrument, InstrumentMaster


def _inst(exchange, sym, token, isin=None):
    return Instrument(
        instrument_token=token,
        exchange_token=token,
        tradingsymbol=sym,
        name=sym,
        expiry=None,
        strike=0.0,
        tick_size=0.05,
        lot_size=1,
        instrument_type="EQ",
        segment=exchange,
        exchange=exchange,
        isin=isin,
    )


def _master(rows):
    return InstrumentMaster(loader=lambda: rows)


def test_pair_by_isin():
    rows = [
        {"instrument_token": 1, "exchange_token": 1, "tradingsymbol": "RELIANCE",
         "name": "RELIANCE", "instrument_type": "EQ", "exchange": "NSE", "segment": "NSE",
         "isin": "INE002A01018", "strike": 0, "tick_size": 0.05, "lot_size": 1},
        {"instrument_token": 2, "exchange_token": 2, "tradingsymbol": "RELIANCE",
         "name": "RELIANCE", "instrument_type": "EQ", "exchange": "BSE", "segment": "BSE",
         "isin": "INE002A01018", "strike": 0, "tick_size": 0.05, "lot_size": 1},
    ]
    master = _master(rows)
    from arb.instruments import build_dual_listed_pairs

    pairs = build_dual_listed_pairs(master, universe="all_matched")
    assert len(pairs) == 1
    assert pairs[0]["symbol"] == "RELIANCE"
    assert pairs[0]["nse_token"] == 1
    assert pairs[0]["bse_token"] == 2
    assert pairs[0]["isin"] == "INE002A01018"


def test_pair_by_tradingsymbol_fallback():
    master = InstrumentMaster(loader=lambda: [])
    master.refresh = lambda: 2  # type: ignore[method-assign]
    master.list_nse_equity = lambda: [_inst("NSE", "TCS", 10)]  # type: ignore[method-assign]
    master.list_bse_equity = lambda: [_inst("BSE", "TCS", 20)]  # type: ignore[method-assign]

    from arb.instruments import build_dual_listed_pairs

    pairs = build_dual_listed_pairs(master, universe="all_matched")
    assert len(pairs) == 1
    assert pairs[0]["symbol"] == "TCS"


def test_nifty50_universe_filter(mocker):
    master = InstrumentMaster(loader=lambda: [])
    master.refresh_if_stale = lambda: False  # type: ignore[method-assign]
    master.list_nse_equity = lambda: [
        _inst("NSE", "RELIANCE", 1),
        _inst("NSE", "ZZZZZZ", 99),
    ]  # type: ignore[method-assign]
    master.list_bse_equity = lambda: [
        _inst("BSE", "RELIANCE", 2),
        _inst("BSE", "ZZZZZZ", 98),
    ]  # type: ignore[method-assign]

    from arb.instruments import build_dual_listed_pairs

    pairs = build_dual_listed_pairs(master, universe="nifty50_dual")
    syms = {p["symbol"] for p in pairs}
    assert "RELIANCE" in syms
    assert "ZZZZZZ" not in syms
