"""Resolve Scout watchlist from DB (UI) or config defaults."""

from __future__ import annotations

from typing import List, Optional, Set

from config import NIFTY_50_SYMBOLS, SCOUT_CONFIG

_WATCHLIST_CACHE: Optional[List[str]] = None


def default_watchlist() -> List[str]:
    return [str(s).upper() for s in (SCOUT_CONFIG.get("watchlist") or [])]


def nifty50_universe() -> List[str]:
    return list(NIFTY_50_SYMBOLS)


def get_watchlist(db=None, *, use_cache: bool = True) -> List[str]:
    """Active watchlist for WS push and scanning."""
    global _WATCHLIST_CACHE
    if db is not None:
        from database.scout_models import ScoutConfigRepo

        saved = ScoutConfigRepo(db).get_watchlist()
        if saved:
            if use_cache:
                _WATCHLIST_CACHE = list(saved)
            return list(saved)
    if use_cache and _WATCHLIST_CACHE is not None:
        return list(_WATCHLIST_CACHE)
    wl = default_watchlist()
    if use_cache:
        _WATCHLIST_CACHE = list(wl)
    return wl


def invalidate_watchlist_cache() -> None:
    global _WATCHLIST_CACHE
    _WATCHLIST_CACHE = None


def watchlist_set(db, symbols: List[str]) -> List[str]:
    from database.scout_models import ScoutConfigRepo

    cleaned = sorted({str(s).upper().strip() for s in symbols if s})
    ScoutConfigRepo(db).set_watchlist(cleaned)
    invalidate_watchlist_cache()
    return cleaned


def is_nifty50(symbol: str) -> bool:
    return str(symbol).upper() in _NIFTY50_SET


_NIFTY50_SET: Set[str] = set(NIFTY_50_SYMBOLS)
