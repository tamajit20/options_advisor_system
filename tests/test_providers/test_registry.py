"""
tests/test_providers/test_registry.py
=====================================

Tests for `providers.registry` mode resolution.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from providers import base
from providers import registry


@pytest.fixture(autouse=True)
def _reset_registry_around_test():
    registry.reset_registry()
    yield
    registry.reset_registry()


def _fake_eod():
    """Build a stand-in NseEodProvider that requires no DB."""
    fake = MagicMock(name="nse_eod_provider")
    fake.name = "nse_eod"
    fake.health.return_value = base.ProviderHealth(
        name="nse_eod", healthy=True, detail="ok"
    )
    return fake


def test_default_mode_resolves_to_nse_eod():
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": ""}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake):
        primary = registry.get_market_data()
    assert primary is fake
    assert primary.name == "nse_eod"


def test_explicit_nse_eod_mode():
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": "nse_eod"}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake):
        assert registry.get_market_data() is fake


def test_unknown_mode_falls_back_to_eod():
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": "garbage"}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake):
        assert registry.get_market_data() is fake


def test_zerodha_mode_falls_back_when_adapter_missing():
    """If the Zerodha provider cannot be constructed (e.g. kiteconnect not
    installed), the registry must gracefully fall back to the EOD provider."""
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": "zerodha"}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake), \
         patch("providers.registry._build_zerodha",
               side_effect=ImportError("kiteconnect not installed")):
        primary = registry.get_market_data()
    assert primary is fake


def test_zerodha_mode_constructs_adapter_when_available():
    """When the Zerodha adapter is importable, the registry returns it (with
    EOD wired in as fallback)."""
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": "zerodha"}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake):
        primary = registry.get_market_data()
    assert primary.name == "zerodha"


def test_get_eod_provider_returns_eod_even_in_live_mode():
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": "zerodha"}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake):
        eod = registry.get_eod_provider()
    assert eod is fake


def test_list_active_providers_includes_health():
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": ""}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake):
        out = registry.list_active_providers()
    assert len(out) >= 1
    assert all(isinstance(h, base.ProviderHealth) for h in out)


def test_registry_is_cached_within_a_session():
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": ""}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake) as builder:
        registry.get_market_data()
        registry.get_market_data()
        registry.get_market_data()
    assert builder.call_count == 1


def test_reset_registry_forces_rebuild():
    fake = _fake_eod()
    with patch.dict(registry.PROVIDERS_CONFIG, {"active": ""}, clear=False), \
         patch("providers.registry._build_nse_eod", return_value=fake) as builder:
        registry.get_market_data()
        registry.reset_registry()
        registry.get_market_data()
    assert builder.call_count == 2
