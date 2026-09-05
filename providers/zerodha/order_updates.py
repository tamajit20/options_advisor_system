"""
Cross-process Kite order postback cache.

The ws_runner container receives order updates on the Kite WebSocket
(``on_order_update``). The dashboard execution thread reads this file to
avoid polling ``order_history`` on every 2s tick.

Order *placement* remains REST-only — Kite does not support placing orders
over WebSocket.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from utils import now_ist


logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"COMPLETE", "REJECTED", "CANCELLED"})
_IN_FLIGHT = frozenset({"PENDING", "OPEN", "TRIGGER PENDING"})

_LOCK = threading.Lock()
_LOCAL: Dict[str, dict] = {}
_LOCAL_EVENTS: Dict[str, threading.Event] = {}

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "kite_order_updates.json"
_MAX_ENTRIES = 300


def order_updates_path() -> Path:
    raw = os.environ.get("OPT_KITE_ORDER_UPDATES_PATH", "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def _read_store() -> Dict[str, dict]:
    path = order_updates_path()
    try:
        if not path.is_file():
            return {}
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        orders = data.get("orders") or {}
        return {str(k): v for k, v in orders.items()} if isinstance(orders, dict) else {}
    except Exception:
        logger.debug("order_updates read failed", exc_info=True)
        return {}


def _write_store(orders: Dict[str, dict]) -> None:
    path = order_updates_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": now_ist().isoformat(),
        "orders": orders,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
    tmp.replace(path)


def persist_order_update(payload: dict) -> None:
    """Store latest Kite order postback (from WS or REST sync)."""
    order_id = str(payload.get("order_id") or "")
    if not order_id:
        return
    snap = {
        "order_id": order_id,
        "status": str(payload.get("status") or "").upper(),
        "filled_quantity": int(payload.get("filled_quantity") or 0),
        "pending_quantity": int(payload.get("pending_quantity") or 0),
        "average_price": payload.get("average_price"),
        "price": payload.get("price"),
        "status_message": payload.get("status_message"),
        "tradingsymbol": payload.get("tradingsymbol"),
        "transaction_type": payload.get("transaction_type"),
        "updated_at": now_ist().isoformat(),
        "raw": payload,
    }
    with _LOCK:
        _LOCAL[order_id] = snap
        ev = _LOCAL_EVENTS.get(order_id)
        if ev is not None:
            ev.set()
        store = _read_store()
        store[order_id] = snap
        if len(store) > _MAX_ENTRIES:
            # Drop oldest by updated_at
            items = sorted(
                store.items(),
                key=lambda kv: kv[1].get("updated_at") or "",
            )
            store = dict(items[-_MAX_ENTRIES:])
        try:
            _write_store(store)
        except Exception:
            logger.debug("order_updates write failed", exc_info=True)


def get_order_snapshot(order_id: str) -> Optional[dict]:
    """Return the newest snapshot — file (WS process) wins over stale in-memory."""
    oid = str(order_id)
    store = _read_store()
    file_row = store.get(oid)
    with _LOCK:
        mem_row = _LOCAL.get(oid)
    if file_row and mem_row:
        if str(file_row.get("updated_at") or "") >= str(mem_row.get("updated_at") or ""):
            return dict(file_row)
        return dict(mem_row)
    if file_row:
        return dict(file_row)
    if mem_row:
        return dict(mem_row)
    return None


def parse_kite_order_row(row: dict) -> dict:
    """Normalize REST ``order_history`` row or WS postback."""
    filled = int(row.get("filled_quantity") or 0)
    qty = int(row.get("quantity") or 0)
    pending = row.get("pending_quantity")
    if pending is None and qty:
        pending = max(0, qty - filled)
    return {
        "order_id": str(row.get("order_id") or ""),
        "status": str(row.get("status") or "").upper(),
        "filled_quantity": filled,
        "pending_quantity": int(pending or 0),
        "average_price": row.get("average_price"),
        "price": row.get("price"),
        "status_message": row.get("status_message"),
        "tradingsymbol": row.get("tradingsymbol"),
        "transaction_type": row.get("transaction_type"),
    }


def wait_for_order_terminal(
    order_id: str,
    *,
    facade: Any,
    max_wait: float,
    poll_interval: float,
    use_ws_cache: bool = True,
) -> dict:
    """Block until order reaches COMPLETE or a terminal fail state.

    Uses WS postback cache when available; falls back to REST ``order_history``.
    Returns normalized order snapshot.
    """
    oid = str(order_id)
    deadline = time.monotonic() + max_wait
    last_row: Optional[dict] = None
    rest_poll_at = 0.0
    ev = threading.Event()
    with _LOCK:
        _LOCAL_EVENTS[oid] = ev

    try:
        while time.monotonic() < deadline:
            row: Optional[dict] = None
            if use_ws_cache:
                row = get_order_snapshot(oid)
                if row and str(row.get("status") or "").upper() not in _TERMINAL:
                    # Non-terminal cache is a hint only — keep REST as source of truth.
                    row = None
            if row is None and time.monotonic() >= rest_poll_at:
                try:
                    history = facade.order_history(oid)
                    if history:
                        raw = history[-1]
                        row = parse_kite_order_row(raw)
                        persist_order_update(raw)
                except Exception:
                    logger.debug("order_history poll failed for %s", oid, exc_info=True)
                rest_poll_at = time.monotonic() + poll_interval

            if row:
                last_row = row
                st = row.get("status") or ""
                if st == "COMPLETE":
                    avg = row.get("average_price") or row.get("price")
                    if avg is None and int(row.get("filled_quantity") or 0) <= 0:
                        raise RuntimeError(f"Order {oid} COMPLETE but average_price missing")
                    return row
                if st in _TERMINAL - {"COMPLETE"}:
                    return row

            remaining = min(poll_interval, max(0.05, deadline - time.monotonic()))
            ev.clear()
            ev.wait(timeout=remaining)
    finally:
        with _LOCK:
            _LOCAL_EVENTS.pop(oid, None)

    if last_row and int(last_row.get("filled_quantity") or 0) > 0:
        return last_row
    st = (last_row or {}).get("status") or "UNKNOWN"
    raise TimeoutError(f"Order {oid} timed out after {max_wait:.0f}s (last status {st})")


def is_terminal_status(status: str) -> bool:
    return str(status or "").upper() in _TERMINAL


def is_in_flight_status(status: str) -> bool:
    return str(status or "").upper() in _IN_FLIGHT
