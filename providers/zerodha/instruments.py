"""
providers/zerodha/instruments.py
================================

Daily-refreshed cache of the Kite instrument master.

Why a cache?
    `kite.instruments(segment)` returns ~30k+ rows. We need it to translate
    `(symbol, expiry, strike, option_type)` into the `instrument_token` and
    full `tradingsymbol` Kite expects in its `quote/ltp/ohlc` calls. Refreshing
    once per day matches Kite's own update cadence (~08:00 IST).

Lookup keys
    Two indexes are built on every refresh:
        1. by `(exchange, tradingsymbol)` — primary key for REST quote calls
        2. by `(symbol, expiry, strike, option_type)` — used by the provider
           when it has only the option attributes from our DB

The cache TTL is 24 hours; `refresh_if_stale()` is a no-op until the TTL
expires.

Memory: each row is a dict; ~80 bytes × 30k = ~2.4 MB. Acceptable.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


logger = logging.getLogger(__name__)


# 24h refresh cadence. Kite regenerates the master at ~08:00 IST.
_DEFAULT_TTL_SECONDS = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Value type
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Instrument:
    instrument_token: int
    exchange_token: int
    tradingsymbol: str
    name: str
    expiry: Optional[date]
    strike: float
    tick_size: float
    lot_size: int
    instrument_type: str   # "EQ", "CE", "PE", "FUT", ...
    segment: str           # "NSE", "NFO", "INDICES", ...
    exchange: str          # "NSE", "NFO", "BSE", ...

    @classmethod
    def from_row(cls, row: dict) -> "Instrument":
        exp = row.get("expiry")
        if isinstance(exp, str) and exp:
            try:
                exp = date.fromisoformat(exp)
            except ValueError:
                exp = None
        elif not isinstance(exp, date):
            exp = None
        return cls(
            instrument_token=int(row["instrument_token"]),
            exchange_token=int(row.get("exchange_token", 0)),
            tradingsymbol=str(row["tradingsymbol"]),
            name=str(row.get("name", "")),
            expiry=exp,
            strike=float(row.get("strike", 0) or 0),
            tick_size=float(row.get("tick_size", 0) or 0),
            lot_size=int(row.get("lot_size", 0) or 0),
            instrument_type=str(row.get("instrument_type", "")),
            segment=str(row.get("segment", "")),
            exchange=str(row.get("exchange", "")),
        )


# ---------------------------------------------------------------------------
# Master cache
# ---------------------------------------------------------------------------
class InstrumentMaster:
    """In-memory instrument cache.

    `loader` is a zero-arg callable returning a list of dict rows in Kite's
    `instruments()` format. In production this is `lambda: facade.instruments()`;
    tests inject a fake list.
    """

    def __init__(
        self,
        loader: Callable[[], Iterable[dict]],
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._loader = loader
        self._ttl = float(ttl_seconds)
        self._lock = threading.RLock()
        self._loaded_at: Optional[float] = None
        self._by_symbol: Dict[Tuple[str, str], Instrument] = {}
        self._by_option: Dict[Tuple[str, date, float, str], Instrument] = {}

    # ------------------------------------------------------------------ refresh
    def refresh(self) -> int:
        """Force a full reload from `loader()`. Returns row count."""
        rows = list(self._loader())
        by_sym: Dict[Tuple[str, str], Instrument] = {}
        by_opt: Dict[Tuple[str, date, float, str], Instrument] = {}
        for row in rows:
            try:
                inst = Instrument.from_row(row)
            except (KeyError, ValueError, TypeError) as exc:
                logger.debug("skip malformed instrument row: %s (%s)", row, exc)
                continue
            by_sym[(inst.exchange, inst.tradingsymbol)] = inst
            if inst.expiry is not None and inst.instrument_type in ("CE", "PE") and inst.name:
                by_opt[(inst.name, inst.expiry, inst.strike, inst.instrument_type)] = inst
        with self._lock:
            self._by_symbol = by_sym
            self._by_option = by_opt
            self._loaded_at = time.monotonic()
        logger.info(
            "instrument master refreshed: %d total, %d option keys", len(by_sym), len(by_opt)
        )
        return len(by_sym)

    def refresh_if_stale(self) -> bool:
        """Refresh only if TTL elapsed (or never loaded). Returns True if a
        refresh actually happened."""
        with self._lock:
            if self._loaded_at is not None and (time.monotonic() - self._loaded_at) < self._ttl:
                return False
        self.refresh()
        return True

    # ------------------------------------------------------------------ lookups
    def get_by_tradingsymbol(self, exchange: str, tradingsymbol: str) -> Optional[Instrument]:
        with self._lock:
            return self._by_symbol.get((exchange, tradingsymbol))

    def get_option(
        self, name: str, expiry: date, strike: float, option_type: str
    ) -> Optional[Instrument]:
        with self._lock:
            return self._by_option.get((name, expiry, float(strike), option_type))

    def list_options(self, name: str, expiry: date) -> List[Instrument]:
        """All CE+PE instruments for `(name, expiry)`. Used by `get_chain`."""
        with self._lock:
            out = [
                inst for (n, e, _s, _t), inst in self._by_option.items()
                if n == name and e == expiry
            ]
        out.sort(key=lambda i: (i.strike, i.instrument_type))
        return out

    def list_expiries(self, name: str) -> List[date]:
        with self._lock:
            seen = {e for (n, e, _s, _t) in self._by_option.keys() if n == name}
        return sorted(seen)

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._loaded_at is not None

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._by_symbol)
