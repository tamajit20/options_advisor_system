"""
providers/zerodha/provider.py
=============================

`MarketDataProvider` implementation backed by Zerodha Kite Connect.

Phase 2a scope (this file): REST-only.
    - Live spot/index quotes via `kite.ltp([...])`
    - Live VIX via `kite.ltp(["NSE:INDIA VIX"])`
    - Live option chain via instrument-master lookup → bulk `kite.ltp([...])`
    - All historical / settled queries delegate to the EOD fallback provider

Phase 2b will add a WebSocket-fed in-memory cache so `get_chain` can serve
ticks instead of REST hits — but the public surface stays identical.

Read precedence (during market hours, today's date):
    1. (Phase 2b) WS cache ≤5s old      — not yet wired
    2. REST `kite.ltp(...)`              — implemented here
    3. EOD fallback (yesterday's close, marked stale)

Token-expiry handling: any `kiteconnect.exceptions.TokenException` (or HTTP 403)
surfaces as `TokenExpiredError` so the suggestion engine can mark the provider
unhealthy and trigger a re-login notification. Other Kite errors become
`ProviderError` and trigger EOD fallback.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from config import PROVIDERS_CONFIG, ZERODHA_API_CONFIG
from exceptions import ProviderError, TokenExpiredError

from ..base import (
    DataSource,
    MarketDataProvider,
    ProviderCapabilities,
    ProviderHealth,
)
from ..cache import TTLCache
from .facade import KiteFacade
from .instruments import Instrument, InstrumentMaster
from .rate_limiter import TokenBucket
from .session import ZerodhaSession, is_token_valid, load_session


logger = logging.getLogger(__name__)


_IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _today_ist() -> date:
    return datetime.now(tz=_IST).date()


def _is_token_exception(exc: BaseException) -> bool:
    """True if `exc` is a Kite token/auth error. Covers both the SDK's typed
    exception and a plain 403 from generic transports."""
    name = exc.__class__.__name__
    if name in ("TokenException",):
        return True
    msg = str(exc).lower()
    if "token" in msg and ("expired" in msg or "invalid" in msg):
        return True
    return False


def _normalise_index_symbol(symbol: str) -> str:
    """Map our internal symbols to Kite's tradingsymbol convention.

    Our DB uses 'NIFTY', 'BANKNIFTY', 'FINNIFTY'; Kite uses 'NIFTY 50',
    'NIFTY BANK', 'NIFTY FIN SERVICE' for the index spot.
    """
    m = {
        "NIFTY":     "NIFTY 50",
        "BANKNIFTY": "NIFTY BANK",
        "FINNIFTY":  "NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NIFTY MID SELECT",
    }
    return m.get(symbol.upper(), symbol)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class ZerodhaProvider:
    """`MarketDataProvider` backed by Kite Connect REST.

    Constructor params:
        eod_fallback   — required `MarketDataProvider` for historical /
                         out-of-hours queries.
        facade         — optional pre-built `KiteFacade`. If None, the provider
                         builds one lazily from `ZERODHA_API_CONFIG` + the
                         persisted `ZerodhaSession`.
        instrument_master — optional `InstrumentMaster`. If None, built lazily.
        cache          — optional `TTLCache`. If None, built from PROVIDERS_CONFIG.
    """

    name: str = "zerodha"

    def __init__(
        self,
        eod_fallback: MarketDataProvider,
        facade: Optional[KiteFacade] = None,
        instrument_master: Optional[InstrumentMaster] = None,
        cache: Optional[TTLCache] = None,
    ):
        if eod_fallback is None:
            raise ValueError("eod_fallback is required (cannot run Zerodha without EOD safety net)")
        self._eod = eod_fallback
        self._facade = facade
        self._instruments = instrument_master
        self._cache = cache or TTLCache(
            default_ttl_seconds=float(PROVIDERS_CONFIG.get("cache_ttl_seconds_quote", 5.0)),
            max_entries=int(PROVIDERS_CONFIG.get("cache_max_entries", 10_000)),
        )
        # Per-endpoint token buckets (Kite verified limits).
        self._rl_quote = TokenBucket(rate_per_sec=1.0)
        self._rl_ltp = TokenBucket(rate_per_sec=10.0)

        self._init_lock = threading.Lock()
        self._last_error: Optional[str] = None
        self._token_expired = False

    # ------------------------------------------------------------------ lazy init
    def _ensure_facade(self) -> KiteFacade:
        with self._init_lock:
            if self._facade is not None:
                return self._facade
            if not ZERODHA_API_CONFIG.get("enabled", True):
                raise ProviderError("Zerodha disabled via OPT_ZERODHA_ENABLED=false")
            api_key = ZERODHA_API_CONFIG.get("api_key", "")
            if not api_key:
                raise ProviderError("OPT_ZERODHA_API_KEY not set")
            session = load_session()
            if session is None or not is_token_valid(session):
                self._token_expired = True
                raise TokenExpiredError(
                    "Zerodha access_token missing or expired; user must re-login on dashboard"
                )
            self._facade = KiteFacade(api_key=api_key, access_token=session.access_token)
            return self._facade

    def _ensure_instruments(self) -> InstrumentMaster:
        with self._init_lock:
            if self._instruments is not None:
                self._instruments.refresh_if_stale()
                return self._instruments
            facade = self._ensure_facade()
            self._instruments = InstrumentMaster(loader=lambda: facade.instruments())
            self._instruments.refresh()
            return self._instruments

    # ------------------------------------------------------------------ error mapping
    def _wrap_call(self, fn: Callable[[], Any], context: str) -> Any:
        """Run `fn()`. Map Kite errors to our exceptions and update health state."""
        try:
            return fn()
        except TokenExpiredError:
            raise
        except Exception as exc:
            if _is_token_exception(exc):
                self._token_expired = True
                self._last_error = f"{context}: token expired"
                raise TokenExpiredError(f"Zerodha token expired during {context}") from exc
            self._last_error = f"{context}: {exc!r}"
            raise ProviderError(f"Zerodha {context} failed: {exc}") from exc

    # ------------------------------------------------------------------ MarketDataProvider API
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            supports_live_quotes=True,
            supports_websocket=False,  # Phase 2b will flip this
            supports_eod=True,         # via `_eod` fallback
            supports_intraday_chain=True,
            notes="REST-only (Phase 2a). EOD fallback via nse_eod for history.",
        )

    def health(self) -> ProviderHealth:
        # Cheap, non-network. We do NOT call profile() here to avoid rate-limit
        # noise; the daily login flow validates token freshness already.
        session = load_session()
        if not ZERODHA_API_CONFIG.get("enabled", True):
            return ProviderHealth(name=self.name, healthy=False, detail="Zerodha disabled")
        if session is None:
            return ProviderHealth(name=self.name, healthy=False, detail="No persisted session — re-login required")
        if not is_token_valid(session):
            return ProviderHealth(
                name=self.name,
                healthy=False,
                detail=f"Token expired (generated {session.generated_at.isoformat()})",
            )
        if self._token_expired:
            return ProviderHealth(name=self.name, healthy=False, detail=f"Token rejected by API: {self._last_error}")
        detail = f"Token valid for user_id={session.user_id}"
        if self._last_error:
            detail += f" | last_error={self._last_error}"
        return ProviderHealth(
            name=self.name,
            healthy=True,
            detail=detail,
            extra={"user_id": session.user_id, "token_generated_at": session.generated_at.isoformat()},
        )

    # ----- spot / index -----
    def get_spot(self, symbol: str, trade_date: Optional[date] = None) -> Optional[dict]:
        # Historical → EOD; only "today" goes live.
        today = _today_ist()
        if trade_date is not None and trade_date != today:
            return self._eod.get_spot(symbol, trade_date)

        try:
            facade = self._ensure_facade()
        except (TokenExpiredError, ProviderError) as exc:
            logger.warning("zerodha get_spot falling back to EOD: %s", exc)
            return self._eod.get_spot(symbol, trade_date)

        kite_symbol = f"NSE:{_normalise_index_symbol(symbol)}"
        cache_key = ("spot", kite_symbol)
        cached, age = self._cache.get_with_age(cache_key)
        if cached is not None:
            return self._stamp_spot(cached, symbol, today, age)

        self._rl_ltp.acquire()
        try:
            data = self._wrap_call(lambda: facade.ltp([kite_symbol]), "ltp(spot)")
        except (TokenExpiredError, ProviderError) as exc:
            logger.warning("zerodha get_spot falling back to EOD: %s", exc)
            return self._eod.get_spot(symbol, trade_date)

        row = data.get(kite_symbol) if isinstance(data, dict) else None
        if not row or "last_price" not in row:
            return self._eod.get_spot(symbol, trade_date)
        self._cache.set(cache_key, row)
        return self._stamp_spot(row, symbol, today, 0.0)

    def _stamp_spot(self, row: dict, symbol: str, trade_date: date, age_seconds: Optional[float]) -> dict:
        last_price = float(row["last_price"])
        return {
            "symbol": symbol,
            "trade_date": trade_date,
            "open_price":  last_price,  # REST LTP doesn't return OHLC; ohlc() is a separate call
            "high_price":  last_price,
            "low_price":   last_price,
            "close_price": last_price,
            "volume":      None,
            "_source":     DataSource.LIVE.value,
            "_provider":   self.name,
            "_freshness_ms": int((age_seconds or 0.0) * 1000),
        }

    # ----- VIX -----
    def get_vix(self, trade_date: Optional[date] = None) -> Optional[dict]:
        today = _today_ist()
        if trade_date is not None and trade_date != today:
            return self._eod.get_vix(trade_date)
        try:
            facade = self._ensure_facade()
        except (TokenExpiredError, ProviderError) as exc:
            logger.warning("zerodha get_vix falling back to EOD: %s", exc)
            return self._eod.get_vix(trade_date)

        kite_symbol = "NSE:INDIA VIX"
        cache_key = ("vix",)
        cached, age = self._cache.get_with_age(cache_key)
        if cached is not None:
            return self._stamp_vix(cached, today, age)

        self._rl_ltp.acquire()
        try:
            data = self._wrap_call(lambda: facade.ltp([kite_symbol]), "ltp(vix)")
        except (TokenExpiredError, ProviderError) as exc:
            logger.warning("zerodha get_vix falling back to EOD: %s", exc)
            return self._eod.get_vix(trade_date)

        row = data.get(kite_symbol) if isinstance(data, dict) else None
        if not row or "last_price" not in row:
            return self._eod.get_vix(trade_date)
        self._cache.set(cache_key, row)
        return self._stamp_vix(row, today, 0.0)

    def _stamp_vix(self, row: dict, trade_date: date, age_seconds: Optional[float]) -> dict:
        lp = float(row["last_price"])
        return {
            "trade_date": trade_date,
            "open_price":  lp, "high_price":  lp, "low_price": lp, "close_price": lp,
            "_source":     DataSource.LIVE.value,
            "_provider":   self.name,
            "_freshness_ms": int((age_seconds or 0.0) * 1000),
        }

    # ----- option chain -----
    def get_chain(self, symbol: str, trade_date: date, expiry: date) -> List[dict]:
        # Historical chains always go through EOD — settled values only.
        if trade_date != _today_ist():
            return self._eod.get_chain(symbol, trade_date, expiry)

        try:
            insts = self._ensure_instruments()
            options = insts.list_options(symbol.upper(), expiry)
        except (TokenExpiredError, ProviderError) as exc:
            logger.warning("zerodha get_chain falling back to EOD: %s", exc)
            return self._eod.get_chain(symbol, trade_date, expiry)
        if not options:
            logger.info("zerodha: no instruments for %s %s; using EOD", symbol, expiry)
            return self._eod.get_chain(symbol, trade_date, expiry)

        # Kite ltp accepts up to 1000 instruments per call, 10 req/sec.
        kite_keys = [f"NFO:{i.tradingsymbol}" for i in options]
        cache_key = ("chain", symbol.upper(), expiry)
        cached, age = self._cache.get_with_age(cache_key)
        if cached is not None:
            return self._build_chain_rows(options, cached, expiry, age)

        self._rl_ltp.acquire()
        try:
            data = self._wrap_call(lambda: self._facade.ltp(kite_keys), "ltp(chain)")  # type: ignore[union-attr]
        except (TokenExpiredError, ProviderError) as exc:
            logger.warning("zerodha get_chain falling back to EOD: %s", exc)
            return self._eod.get_chain(symbol, trade_date, expiry)

        if not isinstance(data, dict) or not data:
            return self._eod.get_chain(symbol, trade_date, expiry)
        self._cache.set(cache_key, data)
        return self._build_chain_rows(options, data, expiry, 0.0)

    def _build_chain_rows(
        self,
        options: List[Instrument],
        ltp_response: Dict[str, Any],
        expiry: date,
        age_seconds: Optional[float],
    ) -> List[dict]:
        rows: List[dict] = []
        freshness_ms = int((age_seconds or 0.0) * 1000)
        for inst in options:
            key = f"NFO:{inst.tradingsymbol}"
            entry = ltp_response.get(key)
            if not entry:
                continue
            try:
                lp = float(entry["last_price"])
            except (KeyError, TypeError, ValueError):
                continue
            rows.append({
                "trade_date":     _today_ist(),
                "symbol":         inst.name,
                "instrument":     "OPTIDX" if inst.name in ("NIFTY", "BANKNIFTY", "FINNIFTY") else "OPTSTK",
                "expiry_date":    expiry,
                "strike":         inst.strike,
                "option_type":    inst.instrument_type,
                "open_price":     lp,
                "high_price":     lp,
                "low_price":      lp,
                "close_price":    lp,
                "settle_price":   lp,
                "contracts":      None,
                "open_interest":  None,
                "change_in_oi":   None,
                "_source":        DataSource.LIVE.value,
                "_provider":      self.name,
                "_freshness_ms":  freshness_ms,
            })
        return rows

    # ----- expiries -----
    def list_expiries(self, symbol: str, trade_date: date) -> List[date]:
        try:
            insts = self._ensure_instruments()
        except (TokenExpiredError, ProviderError) as exc:
            logger.warning("zerodha list_expiries falling back to EOD: %s", exc)
            return self._eod.list_expiries(symbol, trade_date)
        out = [e for e in insts.list_expiries(symbol.upper()) if e >= trade_date]
        return out or self._eod.list_expiries(symbol, trade_date)
