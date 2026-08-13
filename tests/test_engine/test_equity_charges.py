"""Tests for engine/equity_charges.py."""

from __future__ import annotations

from engine.equity_charges import estimate_equity_intraday_charges


def test_equity_intraday_charges_round_trip():
    c = estimate_equity_intraday_charges(entry=1000.0, exit_px=1010.0, qty=20)
    assert c.brokerage > 0
    assert c.stt > 0
    assert c.total > 15


def test_higher_turnover_scales_charges():
    small = estimate_equity_intraday_charges(entry=100.0, exit_px=101.0, qty=10).total
    large = estimate_equity_intraday_charges(entry=1000.0, exit_px=1010.0, qty=100).total
    assert large > small
