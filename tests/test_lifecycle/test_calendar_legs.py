"""Tests for calendar spread expiry resolution in suggestion_engine."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from lifecycle.suggestion_engine import _resolve_calendar_legs


def test_resolve_calendar_legs_picks_near_and_far():
    fo = MagicMock()
    entry_day = date(2026, 5, 20)
    trade_date = date(2026, 5, 19)
    near = date(2026, 5, 29)   # 9 DTE
    far = date(2026, 6, 26)    # 37 DTE
    fo.expiries_for.return_value = [near, far]
    fo.get_chain.side_effect = lambda sym, td, exp: [{"strike": 23000, "option_type": "CE"}]

    out = _resolve_calendar_legs(fo, "NIFTY", trade_date, entry_day)
    assert out is not None
    assert out["near_expiry"] == near
    assert out["far_expiry"] == far
    assert out["near_chain"]
    assert out["far_chain"]


def test_resolve_calendar_legs_rejects_near_above_10_dte():
    """Near ≤10: monthly ~11 DTE cannot be the short leg."""
    fo = MagicMock()
    entry_day = date(2026, 9, 18)
    trade_date = date(2026, 9, 17)
    near = date(2026, 9, 29)   # 11 DTE — blocked at max=10
    far = date(2026, 10, 27)   # 39 DTE
    fo.expiries_for.return_value = [near, far]
    fo.get_chain.side_effect = lambda sym, td, exp: [{"strike": 50000, "option_type": "CE"}]

    assert _resolve_calendar_legs(fo, "BANKNIFTY", trade_date, entry_day) is None


def test_resolve_calendar_legs_none_when_no_far_expiry():
    fo = MagicMock()
    entry_day = date(2026, 5, 20)
    trade_date = date(2026, 5, 19)
    near = date(2026, 5, 29)
    fo.expiries_for.return_value = [near]
    assert _resolve_calendar_legs(fo, "NIFTY", trade_date, entry_day) is None
