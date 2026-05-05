"""NSE live (failsafe) provider — Phase 3 #8.

Pulls intraday option-chain JSON directly from the public NSE endpoint.
Used as a fallback when the primary live provider (Zerodha) is down.
"""
from providers.nse_live.provider import NseLiveChainProvider

__all__ = ["NseLiveChainProvider"]
