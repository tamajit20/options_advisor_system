"""
providers/registry.py
=====================

Provider factory + active-chain assembly.

The system runs in one of two modes (driven by `PROVIDERS_CONFIG["active"]`,
which in turn reads `OPT_PROVIDERS` env var):

    Mode A — `OPT_PROVIDERS=""`           → `nse_eod` only (current behaviour)
    Mode B — `OPT_PROVIDERS="zerodha"`    → `zerodha` for live, `nse_eod` for fallback

The registry returns the **primary** provider — the one suggestion engine,
intraday validator, etc. should call. Fallback chaining (live → REST → stale EOD)
is the responsibility of individual provider implementations, not this layer.
The NSE EOD adapter is always instantiated and is available for any caller
that needs strictly settled historical data.

Mode discovery is lazy — the registry is constructed on first call and cached
process-wide. Tests can use `reset_registry()` to force re-init.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional

from config import PROVIDERS_CONFIG
from .base import MarketDataProvider, ProviderHealth


logger = logging.getLogger(__name__)


_REG_LOCK = threading.Lock()
_PRIMARY: Optional[MarketDataProvider] = None
_EOD_FALLBACK: Optional[MarketDataProvider] = None
_INITIALISED = False


def _build_nse_eod() -> MarketDataProvider:
    """Construct the NSE EOD provider. Imported lazily so test suites that
    don't touch the registry don't pay the DB import cost."""
    from database.connection import SQLServerConnection
    from .nse_eod.provider import NseEodProvider
    return NseEodProvider(connection_factory=SQLServerConnection)


def _build_zerodha(eod_fallback: MarketDataProvider) -> MarketDataProvider:
    """Construct the Zerodha provider. Lazy import so kiteconnect is only
    required when Mode B is active."""
    from .zerodha.provider import ZerodhaProvider
    return ZerodhaProvider(eod_fallback=eod_fallback)


def _initialise() -> None:
    global _PRIMARY, _EOD_FALLBACK, _INITIALISED
    active = (PROVIDERS_CONFIG.get("active") or "").strip().lower()
    eod = _build_nse_eod()
    _EOD_FALLBACK = eod

    if active in ("", "nse_eod"):
        _PRIMARY = eod
        logger.info("Provider registry initialised: primary=nse_eod (Mode A — EOD only)")
    elif active == "zerodha":
        try:
            _PRIMARY = _build_zerodha(eod)
            logger.info("Provider registry initialised: primary=zerodha (Mode B — live data)")
        except Exception:
            logger.warning(
                "OPT_PROVIDERS=zerodha but Zerodha adapter could not be constructed; "
                "falling back to nse_eod.", exc_info=True
            )
            _PRIMARY = eod
    else:
        logger.warning("Unknown OPT_PROVIDERS=%s; falling back to nse_eod", active)
        _PRIMARY = eod

    _INITIALISED = True


def get_market_data() -> MarketDataProvider:
    """Return the primary `MarketDataProvider` for read calls.

    Always returns a usable provider. Falls back to NSE EOD silently if a live
    provider is configured but unavailable (logs a warning the first time).
    """
    global _PRIMARY
    if not _INITIALISED:
        with _REG_LOCK:
            if not _INITIALISED:
                _initialise()
    assert _PRIMARY is not None
    return _PRIMARY


def get_eod_provider() -> MarketDataProvider:
    """Return the NSE EOD provider directly.

    Use this when you need strictly settled, historical data (e.g. IV-rank,
    indicator backfills, the 19:35 verification job). Bypasses the live cache
    even when Mode B is active.
    """
    global _EOD_FALLBACK
    if not _INITIALISED:
        with _REG_LOCK:
            if not _INITIALISED:
                _initialise()
    assert _EOD_FALLBACK is not None
    return _EOD_FALLBACK


def list_active_providers() -> List[ProviderHealth]:
    """Return health snapshots of every provider currently wired into the registry.
    Used by `--provider-status` CLI and by `/api/zerodha/health`."""
    primary = get_market_data()
    eod = get_eod_provider()
    out = [primary.health()]
    if eod is not primary:
        out.append(eod.health())
    return out


def reset_registry() -> None:
    """Test-only: drop the cached registry so the next call re-initialises.
    Production code must NOT call this (provider state may include long-lived
    sockets in future)."""
    global _PRIMARY, _EOD_FALLBACK, _INITIALISED
    with _REG_LOCK:
        _PRIMARY = None
        _EOD_FALLBACK = None
        _INITIALISED = False
