"""Tests for product-scoped WS tick routing."""

from __future__ import annotations

from providers.tick_routing import (
    PRODUCT_OPTIONS_INDEX,
    PRODUCT_OPTIONS_LEG,
    PRODUCT_SCOUT_EQUITY,
    TOPIC_TICK_ARB,
    TOPIC_TICK_INDEX,
    TOPIC_TICK_OPTIONS,
    TOPIC_TICK_SCOUT,
    options_index_symbols,
    options_underlyings,
    resolve_product,
    topic_for_meta,
)
from providers.zerodha.ws_runner import TokenMeta


def test_resolve_product_option_leg():
    meta = TokenMeta(symbol="NIFTY", option_type="CE", strike=22000.0, product=PRODUCT_OPTIONS_LEG)
    assert resolve_product(meta) == PRODUCT_OPTIONS_LEG
    assert topic_for_meta(meta) == TOPIC_TICK_OPTIONS


def test_resolve_product_scout_equity():
    meta = TokenMeta(symbol="BPCL", product=PRODUCT_SCOUT_EQUITY)
    assert resolve_product(meta) == PRODUCT_SCOUT_EQUITY
    assert topic_for_meta(meta) == TOPIC_TICK_SCOUT


def test_options_index_includes_vix():
    assert "VIX" in options_index_symbols()
    assert "VIX" not in options_underlyings()
    assert "NIFTY" in options_underlyings()


def test_resolve_product_options_index_nifty():
    meta = TokenMeta(symbol="NIFTY 50", is_index=True)
    assert resolve_product(meta) == PRODUCT_OPTIONS_INDEX
    assert topic_for_meta(meta) == TOPIC_TICK_INDEX


def test_resolve_product_infers_index_from_symbol():
    meta = TokenMeta(symbol="BANKNIFTY")
    assert resolve_product(meta) == PRODUCT_OPTIONS_INDEX


def test_resolve_product_null_meta_defaults_scout_equity():
    assert resolve_product(None) == PRODUCT_SCOUT_EQUITY


def test_topic_for_product_maps_all_products():
    from providers.tick_routing import (
        PRODUCT_ARB_BSE,
        PRODUCT_ARB_NSE,
        TOPIC_TICK_ARB,
        topic_for_product,
    )

    assert topic_for_product(PRODUCT_OPTIONS_LEG) == TOPIC_TICK_OPTIONS
    assert topic_for_product(PRODUCT_OPTIONS_INDEX) == TOPIC_TICK_INDEX
    assert topic_for_product(PRODUCT_SCOUT_EQUITY) == TOPIC_TICK_SCOUT
    assert topic_for_product(PRODUCT_ARB_NSE) == TOPIC_TICK_ARB
    assert topic_for_product(PRODUCT_ARB_BSE) == TOPIC_TICK_ARB


def test_resolve_product_arb_legs():
    from providers.tick_routing import PRODUCT_ARB_NSE, PRODUCT_ARB_BSE

    meta_nse = TokenMeta(symbol="RELIANCE", product=PRODUCT_ARB_NSE, exchange="NSE")
    meta_bse = TokenMeta(symbol="RELIANCE", product=PRODUCT_ARB_BSE, exchange="BSE")
    assert topic_for_meta(meta_nse) == TOPIC_TICK_ARB
    assert topic_for_meta(meta_bse) == TOPIC_TICK_ARB
