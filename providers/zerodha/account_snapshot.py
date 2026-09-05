"""
Fetch and normalize Kite profile + margin data for dashboard display.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from config import ZERODHA_API_CONFIG
from providers.zerodha.execution_facade import KiteExecutionFacade
from providers.zerodha.session import ZerodhaSession, is_token_valid, load_session

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"at": 0.0, "payload": None}
_CACHE_TTL_SEC = 45


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _segment_margins(raw: dict, segment: str) -> dict:
    seg = raw.get(segment) or {}
    avail = seg.get("available") or {}
    utilised = seg.get("utilised") or {}
    live_balance = _num(avail.get("live_balance"))
    available_cash = _num(avail.get("cash"))
    usable = live_balance if live_balance is not None else available_cash
    return {
        "enabled": bool(seg.get("enabled", True)),
        "net": _num(seg.get("net")),
        "available_cash": available_cash,
        "live_balance": live_balance,
        "usable_balance": usable,
        "opening_balance": _num(avail.get("opening_balance")),
        "utilised_debits": _num(utilised.get("debits")),
        "utilised_span": _num(utilised.get("span")),
        "utilised_exposure": _num(utilised.get("exposure")),
        "utilised_option_premium": _num(utilised.get("option_premium")),
    }


def normalize_account_snapshot(profile: dict, margins: dict) -> dict:
    equity = _segment_margins(margins, "equity")
    commodity = _segment_margins(margins, "commodity")
    usable = equity.get("usable_balance")
    return {
        "user_id": profile.get("user_id"),
        "user_name": profile.get("user_name"),
        "email": profile.get("email"),
        "broker": profile.get("broker"),
        "user_type": profile.get("user_type"),
        "equity": equity,
        "commodity": commodity,
        "available_cash": equity.get("available_cash"),
        "live_balance": equity.get("live_balance"),
        "usable_balance": usable,
        "net": equity.get("net"),
    }


def _build_facade(session: ZerodhaSession) -> KiteExecutionFacade:
    return KiteExecutionFacade(
        api_key=ZERODHA_API_CONFIG["api_key"],
        access_token=session.access_token,
    )


def invalidate_account_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["payload"] = None
        _CACHE["at"] = 0.0


def fetch_account_snapshot(*, force_refresh: bool = False) -> dict:
    """Return profile + margin block for dashboard APIs."""
    session = load_session()
    if session is None or not is_token_valid(session):
        return {"available": False, "reason": "no_valid_session"}

    if not ZERODHA_API_CONFIG.get("api_key"):
        return {"available": False, "reason": "api_key_not_configured"}

    if not force_refresh:
        with _CACHE_LOCK:
            cached = _CACHE.get("payload")
            age = time.monotonic() - float(_CACHE.get("at") or 0)
            if cached is not None and age < _CACHE_TTL_SEC:
                return {**cached, "cached": True}

    try:
        facade = _build_facade(session)
        profile = facade.profile()
        margins = facade.margins()
        body = normalize_account_snapshot(profile, margins)
        body["available"] = True
        body["cached"] = False
        body["fetched_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        stored = {k: v for k, v in body.items() if k != "cached"}
        with _CACHE_LOCK:
            _CACHE["payload"] = stored
            _CACHE["at"] = time.monotonic()
        return body
    except Exception as exc:
        logger.warning("zerodha account snapshot failed: %s", exc)
        return {"available": False, "reason": str(exc)[:200]}
