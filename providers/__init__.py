"""
providers/
==========

Pluggable market-data provider layer.

Architecture (see /memories/session/plan.md):
    - `MarketDataProvider` protocol in `base.py` is the contract every adapter
      satisfies. Adapters: `nse_eod` (EOD bhavcopy via DB), `zerodha` (live REST + WS).
    - The registry (`get_market_data()` below) returns the active provider chain.
      EOD is always available as fallback; live providers layer on top.
    - Read precedence (live providers): in-memory cache → REST fetch → EOD fallback.
    - The provider layer is **read-only**. No order placement, no account/portfolio
      reads. See plan §"MARKET-DATA-ONLY ZERODHA INTEGRATION".

Mode A (current behaviour, no Zerodha):
    OPT_PROVIDERS=""    →    `nse_eod` only

Mode B (Zerodha read-only data feed):
    OPT_PROVIDERS="zerodha"   →   `zerodha` for live, `nse_eod` for history & fallback
"""

from __future__ import annotations

from .base import (
    MarketDataProvider,
    ProviderHealth,
    ProviderCapabilities,
    LiveQuote,
    DataSource,
)
from .registry import get_market_data, reset_registry, list_active_providers

__all__ = [
    "MarketDataProvider",
    "ProviderHealth",
    "ProviderCapabilities",
    "LiveQuote",
    "DataSource",
    "get_market_data",
    "reset_registry",
    "list_active_providers",
]
