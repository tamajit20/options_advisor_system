"""
Read-only live LTP lookup for suggestion / trade legs.

Separate from `lifecycle.zerodha_executor` so the dashboard can show live
prices on a *pending* suggestion without touching the order write path.

Two caches keep polling cheap:
  * the Kite instrument master is held at module scope (24h TTL) — rebuilding
    it per request would re-download ~30k rows;
  * quotes are cached for a few seconds so several cards refreshing at once
    collapse into one Kite call.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from config import ZERODHA_API_CONFIG
from engine.zerodha_price_guard import leg_limit_in_band
from providers.zerodha.facade import KiteFacade
from providers.zerodha.instruments import Instrument, InstrumentMaster
from providers.zerodha.session import is_token_valid, load_session
from utils import now_ist

logger = logging.getLogger(__name__)

_QUOTE_TTL_SEC = 3.0

_MASTER_LOCK = threading.Lock()
_MASTER: Optional[InstrumentMaster] = None
_MASTER_TOKEN: Optional[str] = None

_QUOTE_LOCK = threading.Lock()
_QUOTES: Dict[str, tuple] = {}


def invalidate_caches() -> None:
    """Drop cached master + quotes (used by tests and on session change)."""
    global _MASTER, _MASTER_TOKEN
    with _MASTER_LOCK:
        _MASTER = None
        _MASTER_TOKEN = None
    with _QUOTE_LOCK:
        _QUOTES.clear()


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


def _get_master(facade: KiteFacade, access_token: str) -> InstrumentMaster:
    """Module-scoped instrument master, rebuilt only when the token changes."""
    global _MASTER, _MASTER_TOKEN
    with _MASTER_LOCK:
        if _MASTER is None or _MASTER_TOKEN != access_token:
            _MASTER = InstrumentMaster(loader=lambda: facade.instruments("NFO"))
            _MASTER_TOKEN = access_token
        master = _MASTER
    master.refresh_if_stale()
    return master


def _resolve(leg: dict, master: InstrumentMaster) -> Optional[Instrument]:
    symbol = str(leg.get("symbol") or leg.get("underlying") or "").upper()
    expiry = _as_date(leg.get("expiry_date") or leg.get("expiry"))
    opt = str(leg.get("option_type") or "").upper()
    if not symbol or expiry is None or opt not in ("CE", "PE"):
        return None
    try:
        strike = float(leg["strike"])
    except (KeyError, TypeError, ValueError):
        return None
    inst = master.get_option(symbol, expiry, strike, opt)
    if inst is None:
        master.refresh()
        inst = master.get_option(symbol, expiry, strike, opt)
    return inst


def _cached_ltps(facade: KiteFacade, keys: List[str]) -> Dict[str, float]:
    """LTP per Kite key, served from a short-lived cache where possible."""
    now = time.monotonic()
    out: Dict[str, float] = {}
    missing: List[str] = []
    with _QUOTE_LOCK:
        for key in keys:
            hit = _QUOTES.get(key)
            if hit is not None and (now - hit[0]) < _QUOTE_TTL_SEC:
                out[key] = hit[1]
            else:
                missing.append(key)
    if missing:
        raw = facade.ltp(missing) or {}
        fetched_at = time.monotonic()
        fresh: Dict[str, float] = {}
        for key in missing:
            row = raw.get(key) or {}
            price = row.get("last_price")
            if price is None:
                continue
            try:
                fresh[key] = float(price)
            except (TypeError, ValueError):
                continue
        with _QUOTE_LOCK:
            for key, price in fresh.items():
                _QUOTES[key] = (fetched_at, price)
        out.update(fresh)
    return out


def fetch_leg_live_prices(legs: List[dict]) -> dict:
    """Live LTP for each leg. Never raises — returns a reason when unavailable."""
    if not legs:
        return {"available": False, "reason": "no_legs"}
    if not ZERODHA_API_CONFIG.get("api_key"):
        return {"available": False, "reason": "api_key_not_configured"}
    session = load_session()
    if session is None or not is_token_valid(session):
        return {"available": False, "reason": "no_valid_session"}

    try:
        facade = KiteFacade(
            api_key=ZERODHA_API_CONFIG["api_key"],
            access_token=session.access_token,
        )
        master = _get_master(facade, session.access_token)

        inst_by_leg: Dict[int, Instrument] = {}
        for leg in legs:
            inst = _resolve(leg, master)
            if inst is not None:
                inst_by_leg[int(leg["leg_order"])] = inst
        if not inst_by_leg:
            return {"available": False, "reason": "instruments_not_found"}

        keys = [f"{i.exchange}:{i.tradingsymbol}" for i in inst_by_leg.values()]
        ltps = _cached_ltps(facade, list(dict.fromkeys(keys)))
    except Exception as exc:
        logger.warning("leg live price fetch failed: %s", exc)
        return {"available": False, "reason": str(exc)[:200]}

    out_legs: List[dict] = []
    for leg in legs:
        lo = int(leg["leg_order"])
        inst = inst_by_leg.get(lo)
        ltp = ltps.get(f"{inst.exchange}:{inst.tradingsymbol}") if inst else None
        entry: Dict[str, Any] = {
            "leg_order": lo,
            "tradingsymbol": inst.tradingsymbol if inst else None,
            "ltp": ltp,
            "suggested_price": _num(leg.get("suggested_price")),
            "band_lo": _num(leg.get("suggested_price_low")),
            "band_hi": _num(leg.get("suggested_price_high")),
            "in_band": None,
        }
        if ltp is not None:
            try:
                entry["in_band"] = bool(leg_limit_in_band(leg, ltp))
            except Exception:
                entry["in_band"] = None
        out_legs.append(entry)

    if not any(l["ltp"] is not None for l in out_legs):
        return {"available": False, "reason": "no_live_prices"}
    return {
        "available": True,
        "as_of": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
        "legs": out_legs,
    }


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
