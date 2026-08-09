"""WS subscription helpers for Intraday Scout (equity watchlist only)."""

from __future__ import annotations

from typing import Callable, Iterable, List

from config import SCOUT_CONFIG

EquityLoader = Callable[[], Iterable[str]]


def make_scout_equity_loader() -> EquityLoader:
    """Return NSE equity tradingsymbols from SCOUT_CONFIG watchlist."""

    def _loader() -> List[str]:
        if not SCOUT_CONFIG.get("enabled", True):
            return []
        return [str(s).upper() for s in (SCOUT_CONFIG.get("watchlist") or [])]

    return _loader
