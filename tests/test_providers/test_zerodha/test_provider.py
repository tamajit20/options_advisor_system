"""
tests/test_providers/test_zerodha/test_provider.py
==================================================

Tests for `ZerodhaProvider`. We never touch the network — every test
injects:
    * A mock `KiteFacade` (via `facade=`)
    * A pre-loaded `InstrumentMaster` (via `instrument_master=`)
    * A real `TTLCache`

Historical / fallback behaviour is verified by spying on the EOD provider mock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from providers.base import DataSource, MarketDataProvider, ProviderHealth
from providers.cache import TTLCache
from providers.zerodha.instruments import InstrumentMaster
from providers.zerodha.provider import ZerodhaProvider, _normalise_index_symbol


_IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    return datetime.now(tz=_IST).date()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def eod_mock():
    m = MagicMock(name="eod_provider")
    m.name = "nse_eod"
    m.get_spot.return_value = {"trade_date": date(2026, 5, 1), "close_price": 22950.0}
    m.get_vix.return_value = {"trade_date": date(2026, 5, 1), "close_price": 14.0}
    m.get_chain.return_value = [{"strike": 23000.0, "option_type": "CE", "settle_price": 100.0}]
    m.list_expiries.return_value = [date(2026, 5, 28)]
    return m


@pytest.fixture
def facade_mock():
    f = MagicMock(name="kite_facade")
    f.api_key = "test_key"
    return f


@pytest.fixture
def im_mock():
    """Pre-populated InstrumentMaster avoiding `facade.instruments()` call."""
    rows = [
        {
            "instrument_token": 1, "exchange_token": 2,
            "tradingsymbol": "NIFTY26MAY23000CE", "name": "NIFTY",
            "expiry": "2026-05-28", "strike": 23000.0,
            "tick_size": 0.05, "lot_size": 25,
            "instrument_type": "CE", "segment": "NFO-OPT", "exchange": "NFO",
        },
        {
            "instrument_token": 3, "exchange_token": 4,
            "tradingsymbol": "NIFTY26MAY23000PE", "name": "NIFTY",
            "expiry": "2026-05-28", "strike": 23000.0,
            "tick_size": 0.05, "lot_size": 25,
            "instrument_type": "PE", "segment": "NFO-OPT", "exchange": "NFO",
        },
    ]
    im = InstrumentMaster(loader=lambda: rows)
    im.refresh()
    return im


@pytest.fixture
def provider(eod_mock, facade_mock, im_mock):
    return ZerodhaProvider(
        eod_fallback=eod_mock,
        facade=facade_mock,
        instrument_master=im_mock,
        cache=TTLCache(default_ttl_seconds=5.0),
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_satisfies_protocol(provider):
    assert isinstance(provider, MarketDataProvider)


def test_requires_eod_fallback():
    with pytest.raises(ValueError):
        ZerodhaProvider(eod_fallback=None)  # type: ignore[arg-type]


def test_capabilities(provider):
    caps = provider.capabilities()
    assert caps.name == "zerodha"
    assert caps.supports_live_quotes is True
    assert caps.supports_websocket is False  # Phase 2a
    assert caps.supports_intraday_chain is True


# ---------------------------------------------------------------------------
# Symbol normalisation
# ---------------------------------------------------------------------------
def test_index_symbol_normalisation():
    assert _normalise_index_symbol("NIFTY") == "NIFTY 50"
    assert _normalise_index_symbol("BANKNIFTY") == "NIFTY BANK"
    assert _normalise_index_symbol("FINNIFTY") == "NIFTY FIN SERVICE"
    assert _normalise_index_symbol("INFY") == "INFY"  # passthrough


# ---------------------------------------------------------------------------
# Spot
# ---------------------------------------------------------------------------
def test_get_spot_today_uses_live(provider, facade_mock, eod_mock):
    facade_mock.ltp.return_value = {"NSE:NIFTY 50": {"last_price": 23050.5}}
    out = provider.get_spot("NIFTY")
    assert out is not None
    assert out["close_price"] == 23050.5
    assert out["_source"] == DataSource.LIVE.value
    assert out["_provider"] == "zerodha"
    assert out["_freshness_ms"] == 0
    facade_mock.ltp.assert_called_once_with(["NSE:NIFTY 50"])
    eod_mock.get_spot.assert_not_called()


def test_get_spot_historical_delegates_to_eod(provider, facade_mock, eod_mock):
    yesterday = _today_ist() - timedelta(days=1)
    provider.get_spot("NIFTY", trade_date=yesterday)
    facade_mock.ltp.assert_not_called()
    eod_mock.get_spot.assert_called_once_with("NIFTY", yesterday)


def test_get_spot_caches_repeat_call(provider, facade_mock):
    facade_mock.ltp.return_value = {"NSE:NIFTY 50": {"last_price": 23050.5}}
    provider.get_spot("NIFTY")
    provider.get_spot("NIFTY")
    assert facade_mock.ltp.call_count == 1


def test_get_spot_falls_back_on_missing_data(provider, facade_mock, eod_mock):
    facade_mock.ltp.return_value = {}  # Kite returned nothing
    provider.get_spot("NIFTY")
    eod_mock.get_spot.assert_called_once()


def test_get_spot_falls_back_on_kite_error(provider, facade_mock, eod_mock):
    facade_mock.ltp.side_effect = RuntimeError("network blip")
    provider.get_spot("NIFTY")
    eod_mock.get_spot.assert_called_once()


def test_get_spot_falls_back_when_token_missing(eod_mock, im_mock):
    """No facade injected → constructor tries to build one → no session → fallback."""
    p = ZerodhaProvider(eod_fallback=eod_mock, instrument_master=im_mock)
    with patch("providers.zerodha.provider.load_session", return_value=None):
        p.get_spot("NIFTY")
    eod_mock.get_spot.assert_called_once()


# ---------------------------------------------------------------------------
# VIX
# ---------------------------------------------------------------------------
def test_get_vix_today_uses_live(provider, facade_mock):
    facade_mock.ltp.return_value = {"NSE:INDIA VIX": {"last_price": 14.85}}
    out = provider.get_vix()
    assert out is not None
    assert out["close_price"] == 14.85
    assert out["_source"] == DataSource.LIVE.value


def test_get_vix_historical_delegates(provider, facade_mock, eod_mock):
    provider.get_vix(trade_date=date(2025, 1, 1))
    facade_mock.ltp.assert_not_called()
    eod_mock.get_vix.assert_called_once_with(date(2025, 1, 1))


# ---------------------------------------------------------------------------
# Option chain
# ---------------------------------------------------------------------------
def test_get_chain_today_uses_live(provider, facade_mock):
    facade_mock.ltp.return_value = {
        "NFO:NIFTY26MAY23000CE": {"last_price": 120.5},
        "NFO:NIFTY26MAY23000PE": {"last_price": 95.5},
    }
    rows = provider.get_chain("NIFTY", _today_ist(), date(2026, 5, 28))
    assert len(rows) == 2
    for r in rows:
        assert r["_source"] == DataSource.LIVE.value
        assert r["_provider"] == "zerodha"
        assert r["expiry_date"] == date(2026, 5, 28)
    by_type = {r["option_type"]: r for r in rows}
    assert by_type["CE"]["close_price"] == 120.5
    assert by_type["PE"]["close_price"] == 95.5


def test_get_chain_historical_delegates(provider, facade_mock, eod_mock):
    provider.get_chain("NIFTY", date(2025, 1, 1), date(2025, 1, 30))
    facade_mock.ltp.assert_not_called()
    eod_mock.get_chain.assert_called_once_with("NIFTY", date(2025, 1, 1), date(2025, 1, 30))


def test_get_chain_no_instruments_falls_back(provider, eod_mock):
    """Unknown expiry → no option instruments → EOD fallback."""
    provider.get_chain("NIFTY", _today_ist(), date(2099, 12, 31))
    eod_mock.get_chain.assert_called_once()


def test_get_chain_caches(provider, facade_mock):
    facade_mock.ltp.return_value = {
        "NFO:NIFTY26MAY23000CE": {"last_price": 120.5},
        "NFO:NIFTY26MAY23000PE": {"last_price": 95.5},
    }
    today = _today_ist()
    provider.get_chain("NIFTY", today, date(2026, 5, 28))
    provider.get_chain("NIFTY", today, date(2026, 5, 28))
    assert facade_mock.ltp.call_count == 1


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def test_health_unhealthy_when_no_session(provider):
    with patch("providers.zerodha.provider.load_session", return_value=None):
        h = provider.health()
    assert isinstance(h, ProviderHealth)
    assert h.healthy is False
    assert "re-login" in h.detail.lower()


def test_health_unhealthy_when_token_expired(provider):
    from providers.zerodha.session import ZerodhaSession
    stale = ZerodhaSession(
        api_key="k", access_token="t", user_id="x",
        generated_at=datetime(2025, 1, 1, 9, 0, tzinfo=_IST),
    )
    with patch("providers.zerodha.provider.load_session", return_value=stale):
        h = provider.health()
    assert h.healthy is False
    assert "expired" in h.detail.lower()


def test_health_healthy_with_fresh_session(provider):
    from providers.zerodha.session import ZerodhaSession
    fresh = ZerodhaSession(
        api_key="k", access_token="t", user_id="AB1234",
        generated_at=datetime.now(tz=_IST),
    )
    with patch("providers.zerodha.provider.load_session", return_value=fresh):
        h = provider.health()
    assert h.healthy is True
    assert "AB1234" in h.detail


# ---------------------------------------------------------------------------
# Token-expired error during a call
# ---------------------------------------------------------------------------
def test_token_exception_falls_back_and_marks_provider(provider, facade_mock, eod_mock):
    """Simulate a Kite TokenException at runtime."""
    class TokenException(Exception):
        pass

    facade_mock.ltp.side_effect = TokenException("Invalid access token")
    provider.get_spot("NIFTY")
    eod_mock.get_spot.assert_called_once()

    # After a token error, health should reflect the rejection.
    from providers.zerodha.session import ZerodhaSession
    fresh = ZerodhaSession(
        api_key="k", access_token="t", user_id="x",
        generated_at=datetime.now(tz=_IST),
    )
    with patch("providers.zerodha.provider.load_session", return_value=fresh):
        h = provider.health()
    assert h.healthy is False
    assert "rejected" in h.detail.lower()
