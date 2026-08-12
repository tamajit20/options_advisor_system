"""Scout-only tick classification helpers (equities on the shared WS bus)."""

from __future__ import annotations

from typing import Set

from providers.base import LiveQuote

_INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "VIX", "MIDCPNIFTY"})


def is_scout_equity_tick(q: LiveQuote, watchlist: Set[str]) -> bool:
    """True when tick is an NSE equity on the Scout watchlist (not index/option)."""
    if q is None or q.option_type is not None:
        return False
    sym = str(q.symbol or "").upper()
    if not sym or sym in _INDEX_SYMBOLS:
        return False
    return sym in watchlist
