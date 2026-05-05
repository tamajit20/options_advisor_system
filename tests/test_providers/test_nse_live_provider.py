"""Tests for providers/nse_live/provider.py — Phase 3 #8 NSE failsafe."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
import requests

from providers.base import DataSource
from providers.nse_live.provider import NseLiveChainProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _payload(expiry_str: str = "30-Apr-2026") -> dict:
    """Minimal NSE option-chain JSON shape."""
    return {
        "records": {
            "expiryDates": [expiry_str, "28-May-2026"],
            "data": [
                {
                    "strikePrice": 23000,
                    "expiryDate": expiry_str,
                    "CE": {
                        "lastPrice": 150.5,
                        "openInterest": 1234,
                        "totalTradedVolume": 999,
                    },
                    "PE": {
                        "lastPrice": 80.0,
                        "openInterest": 555,
                        "totalTradedVolume": 110,
                    },
                },
                {
                    # Another expiry — should be filtered out by get_chain
                    "strikePrice": 23000,
                    "expiryDate": "28-May-2026",
                    "CE": {"lastPrice": 200.0, "openInterest": 1, "totalTradedVolume": 2},
                    "PE": {"lastPrice": 100.0, "openInterest": 3, "totalTradedVolume": 4},
                },
            ],
        }
    }


def _make_session(json_payload=None, *, raise_exc: Exception | None = None,
                  status_code: int = 200) -> MagicMock:
    sess = MagicMock(spec=requests.Session)
    if raise_exc is not None:
        sess.get.side_effect = raise_exc
    else:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_payload or {}
        if status_code >= 400:
            resp.raise_for_status.side_effect = requests.HTTPError(
                f"{status_code}", response=resp,
            )
        else:
            resp.raise_for_status.return_value = None
        sess.get.return_value = resp
    return sess


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestGetChain:
    def test_returns_rows_for_matching_expiry(self):
        sess = _make_session(_payload())
        prov = NseLiveChainProvider(session_factory=lambda: sess)
        rows = prov.get_chain("NIFTY", date(2026, 4, 28), date(2026, 4, 30))
        # 1 strike × 2 sides for the matching expiry
        assert len(rows) == 2
        ce = next(r for r in rows if r["option_type"] == "CE")
        assert ce["strike"] == 23000.0
        assert ce["last_price"] == 150.5
        assert ce["settle_price"] == 150.5
        assert ce["open_interest"] == 1234
        assert ce["expiry_date"] == date(2026, 4, 30)
        assert ce["_source"] == DataSource.LIVE.value
        assert ce["_provider"] == "nse_live"

    def test_filters_other_expiries(self):
        sess = _make_session(_payload())
        prov = NseLiveChainProvider(session_factory=lambda: sess)
        rows = prov.get_chain("NIFTY", date(2026, 4, 28), date(2026, 5, 28))
        # Only the second entry matches → 2 sides
        assert len(rows) == 2
        for r in rows:
            assert r["expiry_date"] == date(2026, 5, 28)

    def test_returns_empty_on_fetch_error(self):
        sess = _make_session(raise_exc=requests.ConnectionError("DNS fail"))
        prov = NseLiveChainProvider(session_factory=lambda: sess)
        rows = prov.get_chain("NIFTY", date(2026, 4, 28), date(2026, 4, 30))
        assert rows == []

    def test_returns_empty_on_404(self):
        sess = _make_session(status_code=429)
        prov = NseLiveChainProvider(session_factory=lambda: sess)
        rows = prov.get_chain("NIFTY", date(2026, 4, 28), date(2026, 4, 30))
        assert rows == []

    def test_handles_empty_payload(self):
        sess = _make_session({"records": {}})
        prov = NseLiveChainProvider(session_factory=lambda: sess)
        rows = prov.get_chain("NIFTY", date(2026, 4, 28), date(2026, 4, 30))
        assert rows == []


class TestHealth:
    def test_healthy_when_expiries_present(self):
        sess = _make_session(_payload())
        prov = NseLiveChainProvider(session_factory=lambda: sess)
        h = prov.health()
        assert h.healthy is True
        assert h.extra["expiry_count"] == 2

    def test_unhealthy_when_endpoint_fails(self):
        sess = _make_session(raise_exc=requests.ConnectionError("dead"))
        prov = NseLiveChainProvider(session_factory=lambda: sess)
        h = prov.health()
        assert h.healthy is False
        assert "dead" in h.detail.lower()

    def test_unhealthy_when_payload_empty(self):
        sess = _make_session({"records": {"expiryDates": []}})
        prov = NseLiveChainProvider(session_factory=lambda: sess)
        h = prov.health()
        assert h.healthy is False


class TestCapabilities:
    def test_capabilities_advertise_live_chain(self):
        prov = NseLiveChainProvider(session_factory=_make_session)
        caps = prov.capabilities()
        assert caps.supports_live_quotes is True
        assert caps.supports_intraday_chain is True
        assert caps.supports_eod is False
        assert caps.supports_websocket is False


class TestSessionLazyInit:
    def test_session_created_once_and_reused(self):
        sess = _make_session(_payload())
        factory = MagicMock(return_value=sess)
        prov = NseLiveChainProvider(session_factory=factory)
        prov.get_chain("NIFTY", date(2026, 4, 28), date(2026, 4, 30))
        prov.get_chain("NIFTY", date(2026, 4, 28), date(2026, 4, 30))
        # Session factory invoked only on first call
        assert factory.call_count == 1
