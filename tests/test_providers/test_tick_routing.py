"""Tests for product-scoped WS tick routing."""

from __future__ import annotations

from providers.tick_routing import (
    PRODUCT_OPTIONS_INDEX,
    PRODUCT_OPTIONS_LEG,
    TOPIC_TICK_INDEX,
    TOPIC_TICK_OPTIONS,
    options_index_symbols,
    options_underlyings,
    resolve_product,
    topic_for_meta,
    topic_for_product,
)
from providers.zerodha.ws_runner import TokenMeta


def test_resolve_product_option_leg():
    meta = TokenMeta(symbol="NIFTY", option_type="CE", strike=22000.0, product=PRODUCT_OPTIONS_LEG)
    assert resolve_product(meta) == PRODUCT_OPTIONS_LEG
    assert topic_for_meta(meta) == TOPIC_TICK_OPTIONS


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


def test_resolve_product_null_meta_defaults_index():
    assert resolve_product(None) == PRODUCT_OPTIONS_INDEX


def test_topic_for_product_maps_all_products():
    assert topic_for_product(PRODUCT_OPTIONS_LEG) == TOPIC_TICK_OPTIONS
    assert topic_for_product(PRODUCT_OPTIONS_INDEX) == TOPIC_TICK_INDEX
