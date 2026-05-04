"""
tests/test_providers/test_nse_eod_provider.py
=============================================

Unit tests for `providers.nse_eod.provider.NseEodProvider`.

The adapter is a thin wrapper around `FoEodRepo` / `SpotEodRepo` / `VixRepo`,
so the tests mock the repos and verify the protocol-shape of returned data
plus provenance stamping.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from providers.base import DataSource, MarketDataProvider, ProviderHealth
from providers.nse_eod.provider import NseEodProvider


@pytest.fixture
def fake_db():
    """A fake `SQLServerConnection` instance with no-op connect/close."""
    db = MagicMock(name="fake_db")
    db.connect = MagicMock()
    db.close = MagicMock()
    return db


@pytest.fixture
def provider(fake_db):
    """`NseEodProvider` whose connection_factory always returns the same fake_db."""
    return NseEodProvider(connection_factory=lambda: fake_db)


def test_satisfies_protocol(provider):
    assert isinstance(provider, MarketDataProvider)


def test_capabilities_advertise_eod_only(provider):
    caps = provider.capabilities()
    assert caps.name == "nse_eod"
    assert caps.supports_eod is True
    assert caps.supports_live_quotes is False
    assert caps.supports_websocket is False
    assert caps.supports_intraday_chain is False


def test_get_chain_delegates_to_repo_and_stamps_provenance(provider, fake_db):
    fake_rows = [
        {"strike": 23000.0, "option_type": "CE", "settle_price": 120.5, "open_interest": 1234},
        {"strike": 23000.0, "option_type": "PE", "settle_price": 95.5,  "open_interest": 5678},
    ]
    with patch("database.models.FoEodRepo") as RepoCls:
        RepoCls.return_value.get_chain.return_value = fake_rows
        rows = provider.get_chain("NIFTY", date(2026, 4, 30), date(2026, 5, 14))
    RepoCls.assert_called_once_with(fake_db)
    RepoCls.return_value.get_chain.assert_called_once_with(
        "NIFTY", date(2026, 4, 30), date(2026, 5, 14)
    )
    assert len(rows) == 2
    for r in rows:
        assert r["_source"] == DataSource.EOD.value
        assert r["_provider"] == "nse_eod"
    fake_db.connect.assert_called_once()
    fake_db.close.assert_called_once()


def test_get_chain_returns_empty_when_repo_returns_none(provider):
    with patch("database.models.FoEodRepo") as RepoCls:
        RepoCls.return_value.get_chain.return_value = None
        rows = provider.get_chain("NIFTY", date(2026, 4, 30), date(2026, 5, 14))
    assert rows == []


def test_get_spot_latest_when_no_trade_date(provider):
    with patch("database.models.SpotEodRepo") as RepoCls:
        RepoCls.return_value.latest.return_value = {
            "trade_date": date(2026, 4, 30), "symbol": "NIFTY", "close_price": 23000.0,
        }
        row = provider.get_spot("NIFTY")
    assert row is not None
    assert row["_source"] == DataSource.EOD.value
    assert row["_provider"] == "nse_eod"
    RepoCls.return_value.latest.assert_called_once_with("NIFTY")
    RepoCls.return_value.for_date.assert_not_called()


def test_get_spot_for_date_when_provided(provider):
    with patch("database.models.SpotEodRepo") as RepoCls:
        RepoCls.return_value.for_date.return_value = {
            "trade_date": date(2026, 4, 28), "symbol": "NIFTY", "close_price": 22950.0,
        }
        row = provider.get_spot("NIFTY", trade_date=date(2026, 4, 28))
    assert row is not None
    RepoCls.return_value.for_date.assert_called_once_with("NIFTY", date(2026, 4, 28))
    RepoCls.return_value.latest.assert_not_called()


def test_get_spot_returns_none_when_repo_empty(provider):
    with patch("database.models.SpotEodRepo") as RepoCls:
        RepoCls.return_value.latest.return_value = None
        assert provider.get_spot("NIFTY") is None


def test_get_vix_latest(provider):
    with patch("database.models.VixRepo") as RepoCls:
        RepoCls.return_value.latest.return_value = {
            "trade_date": date(2026, 4, 30), "close_price": 14.2,
        }
        row = provider.get_vix()
    assert row is not None
    assert row["_source"] == DataSource.EOD.value


def test_list_expiries_delegates(provider):
    expected = [date(2026, 5, 7), date(2026, 5, 14)]
    with patch("database.models.FoEodRepo") as RepoCls:
        RepoCls.return_value.expiries_for.return_value = expected
        out = provider.list_expiries("NIFTY", date(2026, 4, 30))
    assert out == expected
    RepoCls.return_value.expiries_for.assert_called_once_with("NIFTY", date(2026, 4, 30))


def test_health_healthy_when_latest_trade_date_present(provider):
    with patch("database.models.FoEodRepo") as RepoCls:
        RepoCls.return_value.latest_trade_date.return_value = date(2026, 4, 30)
        h = provider.health()
    assert isinstance(h, ProviderHealth)
    assert h.healthy is True
    assert "2026-04-30" in h.detail


def test_health_unhealthy_when_no_data(provider):
    with patch("database.models.FoEodRepo") as RepoCls:
        RepoCls.return_value.latest_trade_date.return_value = None
        h = provider.health()
    assert h.healthy is False


def test_health_swallows_exceptions():
    """If the connection_factory itself raises, health() must NOT propagate."""
    def bad_factory():
        raise RuntimeError("DB unreachable")

    p = NseEodProvider(connection_factory=bad_factory)
    h = p.health()
    assert h.healthy is False
    assert "DB unreachable" in h.detail


def test_close_called_even_on_exception(provider, fake_db):
    """A repo error must still close the connection (resource hygiene)."""
    with patch("database.models.FoEodRepo") as RepoCls:
        RepoCls.return_value.get_chain.side_effect = RuntimeError("query failed")
        with pytest.raises(RuntimeError):
            provider.get_chain("NIFTY", date(2026, 4, 30), date(2026, 5, 14))
    fake_db.close.assert_called_once()
