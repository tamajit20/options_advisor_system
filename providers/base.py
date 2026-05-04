"""
providers/base.py
=================

Protocols and value objects for the pluggable market-data provider layer.

This module is intentionally tiny and dependency-free — it is imported by
every adapter and by the registry.

Only `MarketDataProvider` exists. There is no `BrokerProvider` and no
`PositionsReader` — the system is **read-only and market-data-only** with respect
to any broker. See /memories/session/plan.md → "MARKET-DATA-ONLY ZERODHA
INTEGRATION".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import List, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Provenance enum — mirrors the `data_source` column on options_suggestions
# (see plan §"Provenance markers")
# ---------------------------------------------------------------------------
class DataSource(str, Enum):
    EOD = "EOD"          # Settled bhavcopy values (authoritative for history)
    LIVE = "LIVE"        # Live tick/REST snapshot during market hours
    MIXED = "MIXED"      # Some legs live, some EOD (rare; only during fallback)
    UNKNOWN = "UNKNOWN"  # Should never appear; sentinel for migrations


# ---------------------------------------------------------------------------
# Capabilities — providers declare what they can do; callers branch on this
# (See plan §Phase 5 — full per-broker capabilities dict deferred but the
#  shape is defined now so adapters can populate truthful values today.)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderCapabilities:
    name: str                     # e.g. "nse_eod", "zerodha"
    supports_live_quotes: bool    # REST live LTP/quote
    supports_websocket: bool      # streaming ticks
    supports_eod: bool            # historical settled values
    supports_intraday_chain: bool # full option chain during market hours
    notes: str = ""


# ---------------------------------------------------------------------------
# Health — surfaced by /api/zerodha/health and by --provider-status CLI
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderHealth:
    name: str
    healthy: bool
    detail: str
    last_check_at: Optional[datetime] = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# A single live market quote (provider-agnostic)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LiveQuote:
    symbol: str                          # e.g. "NIFTY"
    expiry: Optional[date]               # None for spot/index
    strike: Optional[float]              # None for spot/index
    option_type: Optional[str]           # "CE"/"PE"/None
    last_price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    volume: Optional[int] = None
    open_interest: Optional[int] = None
    timestamp: Optional[datetime] = None  # exchange-provided tick time
    source: DataSource = DataSource.UNKNOWN
    provider: str = ""                   # which adapter served this
    freshness_ms: Optional[int] = None   # tick age in ms (None for EOD)


# ---------------------------------------------------------------------------
# The protocol every adapter satisfies
# ---------------------------------------------------------------------------
@runtime_checkable
class MarketDataProvider(Protocol):
    """Read-only contract for any adapter that can serve market data.

    Implementations:
        - `providers.nse_eod.provider.NseEodProvider`  → wraps existing DB repos
        - `providers.zerodha.provider.ZerodhaProvider` → live Kite REST + WS  (Phase 1+)
    """

    @property
    def name(self) -> str: ...

    def capabilities(self) -> ProviderCapabilities: ...

    def health(self) -> ProviderHealth:
        """Quick self-check. Must NOT raise; return a `ProviderHealth(healthy=False, detail=...)`
        on any failure. Should be cheap (<200 ms) — DB ping or token validity check."""
        ...

    # ----- option chain -----
    def get_chain(
        self, symbol: str, trade_date: date, expiry: date
    ) -> List[dict]:
        """Return a list of dicts (matching the existing FoEodRepo.get_chain shape):
            keys include strike, option_type, settle_price/close_price/last_price,
            open_interest, contracts, expiry_date, ...

        For LIVE providers during market hours, this can return live mid-prices
        instead of `settle_price`. The returned dicts SHOULD include a
        `_source` key set to a `DataSource` value when the provider supports it.
        """
        ...

    # ----- spot / index -----
    def get_spot(self, symbol: str, trade_date: Optional[date] = None) -> Optional[dict]:
        """Return the latest spot row for the symbol, or for a specific
        trade_date if provided. None when unavailable."""
        ...

    # ----- VIX -----
    def get_vix(self, trade_date: Optional[date] = None) -> Optional[dict]:
        """Latest VIX (or for a specific trade_date)."""
        ...

    # ----- expiries -----
    def list_expiries(self, symbol: str, trade_date: date) -> List[date]:
        """Available expiries on/after `trade_date`."""
        ...
