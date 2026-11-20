"""
Process-wide Kite instrument master cache.

Why
----
``kite.instruments(...)`` returns ~30k rows. Building a fresh
``InstrumentMaster`` on every margin preview / execute was re-downloading
that blob mid-session and fighting the live WS rate limit.

This module keeps **one master per (access_token, scope)** in process memory
(~2–3 MB) with the same 24h TTL as ``InstrumentMaster``. Live lookups stay
in-RAM; Kite is contacted only when empty, stale, or the session token
changes.

We intentionally do **not** persist the master to SQL: live token→symbol
lookups must stay microsecond-local, and a daily Kite refresh already matches
Zerodha's own cadence. The WS runner process has its own cache (separate
container); sharing memory across processes is not required.

Scopes
------
- ``"nfo"`` — NFO only (orders, margins, leg LTPs)
- ``"all"`` — full dump (provider chains / index helpers)
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Tuple

from providers.zerodha.facade import KiteFacade
from providers.zerodha.instruments import InstrumentMaster

logger = logging.getLogger(__name__)

_Scope = str  # "nfo" | "all"
_CacheKey = Tuple[str, _Scope]

_LOCK = threading.RLock()
_MASTERS: Dict[_CacheKey, InstrumentMaster] = {}
_TOKENS: Dict[_CacheKey, str] = {}


def _normalize_scope(segment: Optional[str]) -> _Scope:
    if segment is None or str(segment).strip() == "":
        return "all"
    return "nfo" if str(segment).strip().upper() == "NFO" else "all"


def get_shared_instrument_master(
    *,
    api_key: str,
    access_token: str,
    segment: Optional[str] = "NFO",
    force_refresh: bool = False,
    facade: Optional[KiteFacade] = None,
) -> InstrumentMaster:
    """Return a process-shared ``InstrumentMaster``, refreshing only if stale.

    ``segment="NFO"`` (default) loads the F&O book used by execution / quotes.
    ``segment=None`` loads the full master (provider / index helpers).
    """
    if not api_key or not access_token:
        raise ValueError("api_key and access_token are required for instrument master")
    scope = _normalize_scope(segment)
    key: _CacheKey = (access_token, scope)

    with _LOCK:
        master = _MASTERS.get(key)
        if master is None or _TOKENS.get(key) != access_token:
            read = facade or KiteFacade(api_key=api_key, access_token=access_token)

            def _loader() -> list:
                if scope == "nfo":
                    return list(read.instruments("NFO"))
                return list(read.instruments())

            master = InstrumentMaster(loader=_loader)
            _MASTERS[key] = master
            _TOKENS[key] = access_token
            # Drop entries for other tokens so a re-login does not leak masters.
            stale = [k for k in _MASTERS if k[0] != access_token]
            for k in stale:
                _MASTERS.pop(k, None)
                _TOKENS.pop(k, None)

        if force_refresh:
            master.refresh()
        else:
            master.refresh_if_stale()
        return master


def invalidate_shared_instrument_master(
    access_token: Optional[str] = None,
) -> None:
    """Drop cached masters (all scopes, or only for ``access_token``)."""
    with _LOCK:
        if access_token is None:
            _MASTERS.clear()
            _TOKENS.clear()
            logger.info("shared instrument master cache cleared")
            return
        drop = [k for k in _MASTERS if k[0] == access_token]
        for k in drop:
            _MASTERS.pop(k, None)
            _TOKENS.pop(k, None)
        if drop:
            logger.info(
                "shared instrument master cache cleared for token (%d scopes)",
                len(drop),
            )


def shared_instrument_master_stats() -> dict:
    """Diagnostic snapshot for tests / health (no secrets)."""
    with _LOCK:
        return {
            "entries": len(_MASTERS),
            "scopes": sorted({k[1] for k in _MASTERS}),
            "sizes": {k[1]: (m.size if m.loaded else 0) for k, m in _MASTERS.items()},
        }
