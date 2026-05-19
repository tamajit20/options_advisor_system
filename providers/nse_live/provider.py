"""NSE live option-chain failsafe provider — Phase 3 #8.

Wraps the public NSE option-chain JSON endpoint
(`/api/option-chain-indices?symbol=NIFTY`) and exposes it via the
`get_chain(symbol, trade_date, expiry)` shape used elsewhere in the
system. Intended as a *fallback* when the primary live provider
(Zerodha) is unhealthy — NOT the primary source. The endpoint is
unauthenticated but rate-limited and requires the standard NSE
cookie warm-up handled by `downloader.nse_session.make_session`.

Returned rows match the FoEodRepo.get_chain shape so engine code
needn't branch on provider:
    {
        "strike":         float,
        "option_type":    "CE"|"PE",
        "settle_price":   float,    # last_price from NSE feed
        "close_price":    float,    # mirror of last_price
        "last_price":     float,
        "open_interest":  int,
        "expiry_date":    date,
        "_source":        "live",
        "_provider":      "nse_live",
    }
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import List, Optional

import requests

from config import NSE_CONFIG
from providers.base import DataSource, ProviderCapabilities, ProviderHealth
from utils import now_ist

logger = logging.getLogger(__name__)


class NseLiveChainProvider:
    """Failsafe live-chain provider backed by NSE's public JSON endpoint."""

    name: str = "nse_live"

    def __init__(self, session_factory=None) -> None:
        # Lazy import to avoid pulling requests in unit tests that don't need it
        if session_factory is None:
            from downloader.nse_session import make_session
            session_factory = make_session
        self._session_factory = session_factory
        self._session: Optional[requests.Session] = None

    # ---------------------------------------------------------------- helpers
    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = self._session_factory()
        return self._session

    def _fetch_raw(self, symbol: str) -> dict:
        url = NSE_CONFIG["option_chain_url"].format(symbol=symbol.upper())
        sess = self._get_session()
        resp = sess.get(url, timeout=NSE_CONFIG["request_timeout"])
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_expiry(s: str) -> Optional[date]:
        """NSE returns expiries like '30-Apr-2026'."""
        try:
            return datetime.strptime(s, "%d-%b-%Y").date()
        except (ValueError, TypeError):
            return None

    # ---------------------------------------------------------------- API
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_live_quotes=True,
            supports_websocket=False,
            supports_eod=False,
            supports_intraday_chain=True,
            notes="Public NSE JSON endpoint; rate-limited; use only as failsafe.",
        )

    def health(self) -> ProviderHealth:
        try:
            payload = self._fetch_raw("NIFTY")
            records = (payload or {}).get("records", {})
            n_expiries = len(records.get("expiryDates", []) or [])
            healthy = n_expiries > 0
            return ProviderHealth(
                name=self.name,
                healthy=healthy,
                detail=(
                    f"{n_expiries} expiries available"
                    if healthy else "Empty expiryDates from NSE"
                ),
                last_check_at=now_ist(),
                extra={"expiry_count": n_expiries},
            )
        except Exception as exc:
            return ProviderHealth(
                name=self.name,
                healthy=False,
                detail=f"NSE fetch failed: {exc!r}",
                last_check_at=now_ist(),
            )

    def get_chain(
        self, symbol: str, trade_date: date, expiry: date,
    ) -> List[dict]:
        """Fetch intraday option chain. `trade_date` is informational only —
        NSE always returns the current snapshot. `expiry` filters returned
        contracts."""
        try:
            payload = self._fetch_raw(symbol)
        except Exception as exc:
            logger.warning("nse_live: fetch failed for %s: %s", symbol, exc)
            return []

        records = (payload or {}).get("records", {}) or {}
        raw_rows = records.get("data", []) or []
        out: List[dict] = []
        for entry in raw_rows:
            exp = self._parse_expiry(entry.get("expiryDate", ""))
            if exp != expiry:
                continue
            strike = entry.get("strikePrice")
            if strike is None:
                continue
            for side, key in (("CE", "CE"), ("PE", "PE")):
                leg = entry.get(key)
                if not leg:
                    continue
                last_price = float(leg.get("lastPrice") or 0.0)
                snap_ts = now_ist()
                out.append({
                    "strike":         float(strike),
                    "option_type":    side,
                    "settle_price":   last_price,
                    "close_price":    last_price,
                    "last_price":     last_price,
                    "open_interest":  int(leg.get("openInterest") or 0),
                    "contracts":      int(leg.get("totalTradedVolume") or 0),
                    "expiry_date":    exp,
                    "trade_date":     trade_date,
                    "_source":        DataSource.LIVE.value,
                    "_provider":      self.name,
                    "_freshness_ms":  0,
                    "_data_timestamp": snap_ts,
                })
        return out
