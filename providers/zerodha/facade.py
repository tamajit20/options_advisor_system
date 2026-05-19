"""
providers/zerodha/facade.py
===========================

Read-only facade over `kiteconnect.KiteConnect`.

Design principle — **only what we need exists**.

The previous version of this module maintained an ALLOWED / FORBIDDEN list
and used `__getattr__` to proxy calls. That was risky: any new method added
in a future `kiteconnect` release would silently fall through to "unknown"
behaviour, and the long FORBIDDEN list created the impression that the SDK
surface was bounded when it isn't.

This rewrite exposes ONLY the three methods the Zerodha provider actually
uses for market data:

    - `set_access_token(token)`    — wire the daily access token
    - `instruments(exchange=None)` — instrument master for symbol lookup
    - `ltp(keys)`                  — last-traded price for spot / VIX / chain
    - `ohlc(keys)`                 — session OHLC for indices (live trend)
    - `historical_data(...)`       — daily candles for index backfill

Trying to call anything else on the facade raises `AttributeError` because
the attribute simply does not exist. There is no `place_order`,
`positions`, `holdings`, `margins`, `place_gtt`, `place_mf_order` — not
because we block them, but because they were never wired up.

That is the safest possible design: the underlying `KiteConnect` instance
is held in a private attribute (`_kite`) and is never exposed.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional


logger = logging.getLogger(__name__)


class KiteFacade:
    """Read-only market-data facade over `kiteconnect.KiteConnect`.

    Tests inject a stand-in via `kite_client=`:
        f = KiteFacade(api_key="x", kite_client=fake_kite)
    """

    def __init__(
        self,
        api_key: str,
        access_token: Optional[str] = None,
        kite_client: Optional[Any] = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key

        if kite_client is not None:
            # Test / DI path — caller supplies a pre-built (or mocked) KiteConnect.
            self._kite = kite_client
        else:
            # Production path — lazy-import the SDK so unit tests don't need it.
            try:
                from kiteconnect import KiteConnect  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "kiteconnect SDK not installed; pip install kiteconnect>=5.2"
                ) from exc
            self._kite = KiteConnect(api_key=api_key)

        if access_token:
            self.set_access_token(access_token)

    # ------------------------------------------------------------------
    # The only three methods that exist on this facade.
    # Anything else (place_order, positions, holdings, margins, GTTs,
    # mutual funds, alerts, ...) simply does not exist here.
    # ------------------------------------------------------------------
    def set_access_token(self, access_token: str) -> None:
        """Set the daily access token. Called from `__init__` and after a
        login refresh."""
        self._kite.set_access_token(access_token)

    def instruments(self, exchange: Optional[str] = None) -> List[dict]:
        """Return the instrument master (or filtered to a single exchange).
        Used by `InstrumentMaster` to build the symbol → token map."""
        if exchange is None:
            return list(self._kite.instruments())
        return list(self._kite.instruments(exchange))

    def ltp(self, keys: Iterable[str]) -> dict:
        """Return last-traded price for a list of `EXCHANGE:TRADINGSYMBOL`
        keys. The single live-market call we issue."""
        return self._kite.ltp(list(keys))

    def quote(self, keys: Iterable[str]) -> dict:
        """Return full market quote (including `oi`) for a list of
        `EXCHANGE:TRADINGSYMBOL` keys.  Kite rate-limit: 1 req/sec.
        Superset of `ltp` — use this when open_interest is needed."""
        return self._kite.quote(list(keys))

    def ohlc(self, keys: Iterable[str]) -> dict:
        """Session OHLC + last price for index symbols (live trend bar)."""
        return self._kite.ohlc(list(keys))

    def historical_data(
        self,
        instrument_token: int,
        from_date,
        to_date,
        interval: str,
        *,
        continuous: bool = False,
        oi: bool = False,
    ) -> list:
        """Daily (or other) candles for index backfill."""
        return self._kite.historical_data(
            instrument_token,
            from_date,
            to_date,
            interval,
            continuous=continuous,
            oi=oi,
        )

    # ------------------------------------------------------------------
    # Read-only metadata
    # ------------------------------------------------------------------
    @property
    def api_key(self) -> str:
        return self._api_key
