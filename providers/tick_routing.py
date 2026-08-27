"""
providers/tick_routing.py
=========================

Classify WebSocket ticks by product so Options Advisor handlers only see
index and option-leg ticks.

Products
--------
* ``options_index`` — NIFTY / BANKNIFTY / FINNIFTY / VIX (indices for options)
* ``options_leg``   — subscribed option contracts (trades + chain watchlist)

The WS runner publishes each tick to a scoped event-bus topic; handlers subscribe
only to the topics they own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from config import STRATEGY_CONFIG

if TYPE_CHECKING:
    from providers.zerodha.ws_runner import TokenMeta

# TokenMeta.product values (set in subscription_manager reconcile).
PRODUCT_OPTIONS_INDEX = "options_index"
PRODUCT_OPTIONS_LEG = "options_leg"

# Scoped event-bus topics (see providers/event_bus.py).
TOPIC_TICK_OPTIONS = "tick.options"
TOPIC_TICK_INDEX = "tick.index"

_VIX = "VIX"


def options_underlyings() -> frozenset[str]:
    """Index symbols used for options strategies (excludes VIX)."""
    raw = STRATEGY_CONFIG.get("underlyings") or ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    return frozenset(str(s).upper() for s in raw if s)


def options_index_symbols() -> frozenset[str]:
    """Index + VIX spots streamed for Options Advisor."""
    return options_underlyings() | {_VIX}


def resolve_product(meta: Optional["TokenMeta"]) -> str:
    """Infer product from subscription metadata."""
    if meta is None:
        return PRODUCT_OPTIONS_INDEX
    explicit = getattr(meta, "product", None)
    if explicit:
        return str(explicit)
    if meta.option_type and meta.strike is not None:
        return PRODUCT_OPTIONS_LEG
    if meta.is_index or str(meta.symbol or "").upper() in options_index_symbols():
        return PRODUCT_OPTIONS_INDEX
    return PRODUCT_OPTIONS_INDEX


def topic_for_product(product: str) -> str:
    if product == PRODUCT_OPTIONS_LEG:
        return TOPIC_TICK_OPTIONS
    return TOPIC_TICK_INDEX


def topic_for_meta(meta: Optional["TokenMeta"]) -> str:
    return topic_for_product(resolve_product(meta))
