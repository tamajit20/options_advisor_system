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

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union


logger = logging.getLogger(__name__)


# 24h refresh cadence. Kite regenerates the master at ~08:00 IST.
_DEFAULT_TTL_SECONDS = 24 * 60 * 60
# Disk fallback may be used for up to 3 days when Kite rate-limits the download.
_DISK_MAX_AGE_SECONDS = 3 * 24 * 60 * 60


def _default_instruments_cache_dir() -> Path:
    try:
        from config import DASHBOARD_CONFIG
        raw = DASHBOARD_CONFIG.get("data_dir") or "data"
    except Exception:
        raw = os.environ.get("OPT_DATA_DIR", "data")
    return Path(str(raw))


def _serialize_instrument_row(row: dict) -> dict:
    out = dict(row)
    exp = out.get("expiry")
    if isinstance(exp, datetime):
        out["expiry"] = exp.date().isoformat()
    elif isinstance(exp, date):
        out["expiry"] = exp.isoformat()
    return out


def _write_instruments_disk_cache(path: Path, rows: List[dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "saved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "count": len(rows),
            "rows": [_serialize_instrument_row(r) for r in rows if isinstance(r, dict)],
        }
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.warning("instrument disk cache write failed: %s", path, exc_info=True)


def _read_instruments_disk_cache(
    path: Path,
    *,
    max_age_sec: float = _DISK_MAX_AGE_SECONDS,
) -> Optional[List[dict]]:
    try:
        if not path.is_file():
            return None
        age = time.time() - path.stat().st_mtime
        if age > max_age_sec:
            logger.warning(
                "instrument disk cache too old (%.0fh): %s", age / 3600.0, path,
            )
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        logger.info(
            "instrument disk cache hit: %d rows from %s (age %.0fm)",
            len(rows), path, age / 60.0,
        )
        return rows
    except Exception:
        logger.warning("instrument disk cache read failed: %s", path, exc_info=True)
        return None


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
    isin: Optional[str] = None

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
        isin_raw = row.get("isin")
        isin = str(isin_raw).strip().upper() if isin_raw else None
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
            isin=isin or None,
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
        *,
        disk_cache_path: Optional[Union[str, Path]] = None,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._loader = loader
        self._ttl = float(ttl_seconds)
        self._disk_cache_path = Path(disk_cache_path) if disk_cache_path else None
        self._lock = threading.RLock()
        self._loaded_at: Optional[float] = None
        self._by_symbol: Dict[Tuple[str, str], Instrument] = {}
        self._by_option: Dict[Tuple[str, date, float, str], Instrument] = {}

    # ------------------------------------------------------------------ refresh
    @staticmethod
    def _is_rate_limit_error(exc: BaseException) -> bool:
        msg = str(exc) or ""
        if "too many requests" in msg.lower() or "rate limit" in msg.lower():
            return True
        return exc.__class__.__name__ == "NetworkException"

    def _load_rows_with_retry(self, *, attempts: int = 5) -> list:
        """Load instrument rows; back off on Kite 'Too many requests'."""
        last_exc: Optional[BaseException] = None
        # Instruments download is large — Kite needs a longer cool-down than
        # quote/ltp when we have already been hammering margins/preview.
        waits = (5.0, 15.0, 30.0, 45.0)
        for i in range(max(1, attempts)):
            try:
                return list(self._loader())
            except Exception as exc:
                last_exc = exc
                if not self._is_rate_limit_error(exc) or i == attempts - 1:
                    raise
                wait = waits[min(i, len(waits) - 1)]
                logger.warning(
                    "instrument master rate-limited (%s) — retry in %.1fs",
                    exc, wait,
                )
                time.sleep(wait)
        if last_exc is not None:
            raise last_exc
        return []

    def _apply_rows(self, rows: List[dict]) -> int:
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
        return len(by_sym)

    def _load_from_disk(self) -> bool:
        if self._disk_cache_path is None:
            return False
        rows = _read_instruments_disk_cache(self._disk_cache_path)
        if not rows:
            return False
        n = self._apply_rows(rows)
        logger.info(
            "instrument master loaded from disk: %d total (%s)",
            n, self._disk_cache_path,
        )
        return n > 0

    def refresh(self) -> int:
        """Force a full reload from `loader()`. Returns row count."""
        rows = self._load_rows_with_retry()
        n = self._apply_rows(rows)
        if self._disk_cache_path is not None:
            _write_instruments_disk_cache(self._disk_cache_path, list(rows))
        with self._lock:
            opt_n = len(self._by_option)
        logger.info(
            "instrument master refreshed: %d total, %d option keys",
            n, opt_n,
        )
        return n

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded_at is not None and bool(self._by_symbol)

    def refresh_if_stale(self) -> bool:
        """Refresh only if TTL elapsed (or never loaded). Returns True if a
        refresh actually happened.

        If the master has never loaded and Kite rate-limits the download, try
        the on-disk cache before failing — empty token maps zero WS
        subscriptions and freeze the header on stale EOD.

        If a previous load exists and the refresh fails, keep the stale map
        and return False — better a day-old token list than zero subscriptions.
        """
        with self._lock:
            if self._loaded_at is not None and (time.monotonic() - self._loaded_at) < self._ttl:
                return False
        try:
            self.refresh()
        except Exception:
            with self._lock:
                never_loaded = self._loaded_at is None
            if never_loaded:
                if self._load_from_disk():
                    logger.warning(
                        "instrument master using disk cache after Kite failure",
                    )
                    return True
                logger.error(
                    "instrument master still empty after failed refresh — will retry",
                    exc_info=True,
                )
                raise
            logger.warning(
                "instrument master refresh failed — keeping stale cache",
                exc_info=True,
            )
            return False
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

    def list_nse_equity(self) -> List[Instrument]:
        """All NSE cash equities (EQ) — refreshed from Kite instrument master."""
        return self._list_equity("NSE")

    def list_bse_equity(self) -> List[Instrument]:
        """All BSE cash equities (EQ)."""
        return self._list_equity("BSE")

    def list_nfo_futures(self, name: str) -> List[Instrument]:
        """All NFO FUT for underlying `name` (Kite `name` = EQ tradingsymbol)."""
        sym = str(name).upper()
        with self._lock:
            out = [
                inst for inst in self._by_symbol.values()
                if inst.exchange == "NFO"
                and inst.instrument_type == "FUT"
                and inst.name.upper() == sym
            ]
        out.sort(key=lambda i: (i.expiry or date.max))
        return out

    def _list_equity(self, exchange: str) -> List[Instrument]:
        with self._lock:
            out = [
                inst for inst in self._by_symbol.values()
                if inst.exchange == exchange and inst.instrument_type == "EQ"
            ]
        out.sort(key=lambda i: i.tradingsymbol)
        return out

    @property
    def loaded_at_monotonic(self) -> Optional[float]:
        with self._lock:
            return self._loaded_at

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._loaded_at is not None

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._by_symbol)
