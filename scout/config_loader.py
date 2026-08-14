"""Resolve Scout watchlist and settings from DB (UI) or config defaults."""

from __future__ import annotations

import logging
from typing import List, Optional

from config import SCOUT_CONFIG
from scout.settings_schema import default_scout_settings, merge_scout_settings, validate_scout_settings

logger = logging.getLogger(__name__)

_WATCHLIST_CACHE: Optional[List[str]] = None
_SETTINGS_CACHE: Optional[dict] = None


def default_watchlist() -> List[str]:
    return [str(s).upper() for s in (SCOUT_CONFIG.get("watchlist") or [])]


def default_automation() -> dict:
    s = default_scout_settings()
    return {
        "auto_execute_signals": s["auto_execute_signals"],
        "auto_close_trades": s["auto_close_trades"],
        "auto_trade_quantity": s["auto_trade_quantity"],
    }


def nifty50_universe() -> List[str]:
    from scout.instruments import nifty50_symbols
    return nifty50_symbols()


def get_watchlist(db=None, *, use_cache: bool = True) -> List[str]:
    """Active watchlist for WS push and scanning."""
    global _WATCHLIST_CACHE
    if use_cache and _WATCHLIST_CACHE is not None:
        return list(_WATCHLIST_CACHE)
    if db is not None:
        from database.scout_models import ScoutConfigRepo

        saved = ScoutConfigRepo(db).get_watchlist()
        if saved:
            _WATCHLIST_CACHE = list(saved)
            return list(saved)
    wl = default_watchlist()
    _WATCHLIST_CACHE = list(wl)
    return wl


def _load_scout_settings_from_db(db) -> dict:
    from database.scout_models import ScoutConfigRepo

    repo = ScoutConfigRepo(db)
    saved = repo.get_settings()
    if saved is None:
        legacy = repo.get_automation()
        if legacy:
            saved = {**default_scout_settings(), **legacy}
    return merge_scout_settings(saved)


def get_scout_settings(db=None, *, use_cache: bool = True) -> dict:
    """Full Scout settings — served from in-process cache unless cache bypassed."""
    global _SETTINGS_CACHE
    if use_cache and _SETTINGS_CACHE is not None:
        return dict(_SETTINGS_CACHE)

    if db is not None:
        merged = _load_scout_settings_from_db(db)
    else:
        merged = default_scout_settings()

    _SETTINGS_CACHE = dict(merged)
    return dict(merged)


def reload_scout_settings(db) -> dict:
    """Force reload from DB and refresh the in-process cache."""
    return get_scout_settings(db, use_cache=False)


def get_automation(db=None, *, use_cache: bool = True) -> dict:
    """Automation toggles (subset of scout settings)."""
    s = get_scout_settings(db, use_cache=use_cache)
    return {
        "auto_execute_signals": bool(s.get("auto_execute_signals")),
        "auto_close_trades": bool(s.get("auto_close_trades")),
        "auto_trade_quantity": max(1, int(s.get("auto_trade_quantity", 1))),
    }


def set_scout_settings(db, settings: dict, *, updated_by: str = "ui") -> dict:
    from database.scout_models import ScoutConfigRepo

    cleaned = validate_scout_settings(settings)
    ScoutConfigRepo(db).set_settings(cleaned, updated_by=updated_by)
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = dict(cleaned)
    return cleaned


def set_automation(db, settings: dict, *, updated_by: str = "ui") -> dict:
    """Merge automation patch into full settings and save."""
    current = reload_scout_settings(db)
    patch = {
        "auto_execute_signals": bool(settings.get("auto_execute_signals", current["auto_execute_signals"])),
        "auto_close_trades": bool(settings.get("auto_close_trades", current["auto_close_trades"])),
    }
    if "auto_trade_quantity" in settings:
        patch["auto_trade_quantity"] = max(1, int(settings["auto_trade_quantity"]))
    return set_scout_settings(db, {**current, **patch}, updated_by=updated_by)


def invalidate_watchlist_cache() -> None:
    global _WATCHLIST_CACHE
    _WATCHLIST_CACHE = None


def invalidate_settings_cache() -> None:
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None


def invalidate_automation_cache() -> None:
    invalidate_settings_cache()


def watchlist_set(db, symbols: List[str]) -> List[str]:
    from database.scout_models import ScoutConfigRepo
    from scout.instruments import valid_nse_symbols

    raw = [str(s).upper().strip() for s in symbols if s]
    try:
        cleaned = valid_nse_symbols(raw)
    except Exception:
        cleaned = sorted(set(raw))
    if len(cleaned) < len(set(raw)):
        logger.warning(
            "scout watchlist: dropped %d unknown symbol(s) not in NSE master",
            len(set(raw)) - len(cleaned),
        )
    ScoutConfigRepo(db).set_watchlist(cleaned)
    invalidate_watchlist_cache()
    return cleaned


def is_nifty50(symbol: str) -> bool:
    from scout.index_constituents import get_nifty50_symbols

    return str(symbol).upper() in {s.upper() for s in get_nifty50_symbols()}
