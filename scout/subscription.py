"""WS subscription helpers for Intraday Scout (equity watchlist only).

Scout equities are tagged ``product=scout_equity`` in SubscriptionManager and
published on ``tick.scout`` — they never reach Options tick handlers.
"""

from __future__ import annotations

from typing import Callable, Iterable, List

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from scout.config_loader import get_watchlist

EquityLoader = Callable[[], Iterable[str]]


def make_scout_equity_loader(db: SQLServerConnection) -> EquityLoader:
    """Return NSE equity tradingsymbols from DB-backed watchlist."""

    def _loader() -> List[str]:
        if not SCOUT_CONFIG.get("enabled", True):
            return []
        return get_watchlist(db)

    return _loader
