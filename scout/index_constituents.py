"""Nifty 50 (and related) index constituents — synced from NSE, cached on disk."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from config import NIFTY_50_SYMBOLS, NSE_CONFIG, PATHS
from downloader.nse_session import fetch_with_retry, make_session
from utils import now_ist

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "scout_index_constituents.json"
_NIFTY50_INDEX = "NIFTY 50"
_STALE_AFTER = timedelta(hours=24)

_lock = threading.Lock()
_memory: Optional[List[str]] = None


def _data_dir() -> Path:
    data_dir = Path(PATHS.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = Path(__file__).resolve().parents[1] / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _cache_path() -> Path:
    return _data_dir() / _CACHE_FILENAME


def default_nifty50_symbols() -> List[str]:
    """Static fallback when NSE sync is unavailable (see config.NIFTY_50_SYMBOLS)."""
    return list(NIFTY_50_SYMBOLS)


def _nse_index_url(index_name: str) -> str:
    base = NSE_CONFIG.get(
        "index_constituents_url",
        "https://www.nseindia.com/api/equity-stockIndices?index={index}",
    )
    return base.format(index=quote(index_name, safe=""))


def parse_nse_index_payload(payload: dict) -> List[str]:
    """Extract EQ symbols from NSE equity-stockIndices JSON."""
    rows = payload.get("data") or []
    ranked: list[tuple[int, str]] = []
    for row in rows:
        if str(row.get("series", "")).upper() != "EQ":
            continue
        sym = str(row.get("symbol", "")).upper().strip()
        if not sym:
            continue
        try:
            priority = int(row.get("priority", 9999))
        except (TypeError, ValueError):
            priority = 9999
        ranked.append((priority, sym))
    ranked.sort(key=lambda x: (x[0], x[1]))
    seen: set[str] = set()
    out: List[str] = []
    for _, sym in ranked:
        if sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def fetch_nifty50_from_nse() -> List[str]:
    session = make_session()
    url = _nse_index_url(_NIFTY50_INDEX)
    resp = fetch_with_retry(session, url)
    if resp is None:
        raise RuntimeError(f"NSE index constituents request failed: {url}")
    payload = resp.json()
    symbols = parse_nse_index_payload(payload)
    if len(symbols) < 40:
        raise RuntimeError(
            f"NSE returned too few Nifty 50 symbols ({len(symbols)}) — refusing to cache"
        )
    return symbols


def _parse_updated_at(raw: object) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _load_cache() -> Optional[dict]:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read index constituents cache: %s", exc)
        return None


def _cache_is_stale(updated_at: Optional[datetime]) -> bool:
    if updated_at is None:
        return True
    now = now_ist()
    if updated_at.date() < now.date():
        return True
    return (now - updated_at) > _STALE_AFTER


def _write_cache(symbols: List[str], *, source: str) -> None:
    payload = {
        "updated_at": now_ist().isoformat(sep="T", timespec="seconds"),
        "source": source,
        "index": _NIFTY50_INDEX,
        "symbols": symbols,
    }
    path = _cache_path()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _apply_symbols(symbols: List[str]) -> List[str]:
    global _memory
    clean = [str(s).upper().strip() for s in symbols if s]
    _memory = clean
    from scout.index_groups import invalidate_index_tags_cache

    invalidate_index_tags_cache()
    return clean


def refresh_nifty50_constituents(*, force: bool = False) -> List[str]:
    """Fetch Nifty 50 from NSE and persist to disk. Returns symbol list."""
    with _lock:
        if not force:
            cached = _load_cache()
            if cached:
                updated_at = _parse_updated_at(cached.get("updated_at"))
                syms = cached.get("symbols") or []
                if syms and not _cache_is_stale(updated_at):
                    return _apply_symbols(syms)

        try:
            fetched = fetch_nifty50_from_nse()
            _write_cache(fetched, source="nse")
            logger.info("Nifty 50 constituents synced from NSE (%d symbols)", len(fetched))
            return _apply_symbols(fetched)
        except Exception as exc:
            logger.warning("NSE Nifty 50 sync failed: %s", exc)
            cached = _load_cache()
            if cached and cached.get("symbols"):
                logger.info("Using cached Nifty 50 constituents (%d symbols)", len(cached["symbols"]))
                return _apply_symbols(cached["symbols"])
            fallback = default_nifty50_symbols()
            logger.info("Using config fallback Nifty 50 list (%d symbols)", len(fallback))
            return _apply_symbols(fallback)


def get_nifty50_symbols(*, force_refresh: bool = False) -> List[str]:
    """Current Nifty 50 list — memory/cache/NSE sync with config fallback."""
    global _memory
    with _lock:
        if _memory is not None and not force_refresh:
            return list(_memory)

    if force_refresh:
        return refresh_nifty50_constituents(force=True)

    cached = _load_cache()
    if cached and cached.get("symbols"):
        updated_at = _parse_updated_at(cached.get("updated_at"))
        if not _cache_is_stale(updated_at):
            return _apply_symbols(cached["symbols"])

    # Cache missing or stale — try a background-safe refresh in-process.
    return refresh_nifty50_constituents(force=False)


def run_scout_index_constituents(_db, _trade_date=None) -> int:
    """Scheduler entry — refresh Nifty 50 from NSE before market open."""
    symbols = refresh_nifty50_constituents(force=True)
    return len(symbols)
