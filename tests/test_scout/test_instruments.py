"""Tests for scout NSE equity universe."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from providers.zerodha.instruments import Instrument, InstrumentMaster


def _eq(sym: str, name: str = "") -> Instrument:
    return Instrument(
        instrument_token=1,
        exchange_token=1,
        tradingsymbol=sym,
        name=name or sym,
        expiry=None,
        strike=0.0,
        tick_size=0.05,
        lot_size=1,
        instrument_type="EQ",
        segment="NSE",
        exchange="NSE",
    )


def test_list_nse_equity_filters_eq_only():
    rows = [
        {"instrument_token": 1, "exchange_token": 1, "tradingsymbol": "RELIANCE",
         "name": "Reliance", "instrument_type": "EQ", "segment": "NSE", "exchange": "NSE",
         "strike": 0, "tick_size": 0.05, "lot_size": 1},
        {"instrument_token": 2, "exchange_token": 2, "tradingsymbol": "NIFTY24APRFUT",
         "name": "NIFTY", "instrument_type": "FUT", "segment": "NFO-FUT", "exchange": "NFO",
         "expiry": "2026-04-24", "strike": 0, "tick_size": 0.05, "lot_size": 50},
    ]
    master = InstrumentMaster(loader=lambda: rows)
    master.refresh()
    eq = master.list_nse_equity()
    assert len(eq) == 1
    assert eq[0].tradingsymbol == "RELIANCE"


def test_nse_equity_universe_search(mocker):
    from scout import instruments as scout_inst

    master = MagicMock()
    master.list_nse_equity.return_value = [
        _eq("RELIANCE", "Reliance Industries"),
        _eq("TCS", "Tata Consultancy"),
    ]
    master.loaded_at_monotonic = 1000.0
    mocker.patch("scout.instruments._session_master", return_value=master)

    page, total, _ = scout_inst.nse_equity_universe(search="TCS", offset=0, limit=10)
    assert total == 1
    assert page[0]["symbol"] == "TCS"


def test_equity_display_name_skips_numeric(mocker):
    from scout.instruments import _equity_display_name

    inst = _eq("RELIANCE", "738561")
    assert _equity_display_name(inst) == ""
    inst2 = _eq("RELIANCE", "Reliance Industries")
    assert _equity_display_name(inst2) == "Reliance Industries"
