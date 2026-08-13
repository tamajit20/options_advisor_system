"""Tests for scout/entry_pricing.py."""

from __future__ import annotations

from scout.entry_pricing import entry_limit_price


def test_entry_limit_price_buy_uses_entry_max():
    sig = {"action": "BUY", "ltp": 100.0}
    enriched = {"entry_max": 100.3, "entry_min": 99.8}
    assert entry_limit_price(enriched, sig, 100.0) == 100.3


def test_entry_limit_price_sell_uses_entry_min():
    sig = {"action": "SELL", "ltp": 100.0}
    enriched = {"entry_max": 100.3, "entry_min": 99.7}
    assert entry_limit_price(enriched, sig, 100.0) == 99.7
