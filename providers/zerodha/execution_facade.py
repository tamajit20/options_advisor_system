"""
providers/zerodha/execution_facade.py
=====================================

Write-path facade for Kite Connect order APIs. Kept separate from the
read-only `KiteFacade` so market-data code cannot accidentally place orders.

Only constructed when Zerodha execution is explicitly enabled.

Order placement is process-wide rate-limited (default 9/sec, under Kite's
10 orders/sec cap). See ``ZERODHA_EXECUTION_CONFIG["orders_per_sec"]``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

from providers.zerodha.rate_limiter import SlidingWindowCap


logger = logging.getLogger(__name__)

# Kite Connect hard limit for order APIs (place / modify / cancel).
_KITE_ORDER_HARD_CAP = 10

_order_cap: Optional[SlidingWindowCap] = None
_order_cap_lock = threading.Lock()


def _configured_orders_per_sec() -> int:
    try:
        from config import ZERODHA_EXECUTION_CONFIG
        raw = ZERODHA_EXECUTION_CONFIG.get("orders_per_sec", 9)
        n = int(raw)
    except Exception:
        n = 9
    if n < 1:
        n = 1
    if n > _KITE_ORDER_HARD_CAP:
        logger.warning(
            "orders_per_sec=%s exceeds Kite's %s/s limit; clamping to %s",
            n, _KITE_ORDER_HARD_CAP, _KITE_ORDER_HARD_CAP,
        )
        n = _KITE_ORDER_HARD_CAP
    return n


def order_rate_cap() -> SlidingWindowCap:
    """Process-wide sliding 1s window for place/modify/cancel."""
    global _order_cap
    max_n = _configured_orders_per_sec()
    with _order_cap_lock:
        if _order_cap is None:
            _order_cap = SlidingWindowCap(max_n, window_sec=1.0)
        else:
            _order_cap.set_max(max_n)
        return _order_cap


def _acquire_order_slot() -> None:
    cap = order_rate_cap()
    if not cap.try_acquire():
        logger.info(
            "Zerodha order rate cap (%s/s) reached — waiting for next slot",
            cap.max_per_window,
        )
        cap.acquire()


def reset_order_rate_cap_for_tests() -> None:
    """Drop the shared cap (tests only)."""
    global _order_cap
    with _order_cap_lock:
        _order_cap = None


class KiteExecutionFacade:
    """Minimal order-placement wrapper over `kiteconnect.KiteConnect`."""

    def __init__(
        self,
        api_key: str,
        access_token: str,
        *,
        kite_client: Optional[Any] = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if not access_token:
            raise ValueError("access_token is required")
        self._api_key = api_key

        if kite_client is not None:
            self._kite = kite_client
        else:
            try:
                from kiteconnect import KiteConnect  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "kiteconnect SDK not installed; pip install kiteconnect>=5.2"
                ) from exc
            self._kite = KiteConnect(api_key=api_key)

        self._kite.set_access_token(access_token)

    @property
    def api_key(self) -> str:
        return self._api_key

    def place_order(self, **kwargs) -> str:
        _acquire_order_slot()
        order_id = self._kite.place_order(**kwargs)
        return str(order_id)

    def modify_order(self, *, order_id: str, **kwargs) -> str:
        _acquire_order_slot()
        return str(self._kite.modify_order(order_id=order_id, **kwargs))

    def cancel_order(self, *, order_id: str, variety: str = "regular") -> str:
        _acquire_order_slot()
        return str(self._kite.cancel_order(variety=variety, order_id=order_id))

    def order_history(self, order_id: str) -> List[dict]:
        return list(self._kite.order_history(order_id))

    def orders(self) -> List[dict]:
        return list(self._kite.orders())

    def ltp(self, keys) -> dict:
        return self._kite.ltp(list(keys))

    def quote(self, keys) -> dict:
        return self._kite.quote(list(keys))

    def profile(self) -> dict:
        return dict(self._kite.profile())

    def margins(self, segment: Optional[str] = None) -> dict:
        if segment:
            return dict(self._kite.margins(segment=segment))
        return dict(self._kite.margins())

    def order_margins(self, orders: list) -> Any:
        return self._kite.order_margins(list(orders))

    def basket_order_margins(self, orders: list, *, consider_positions: bool = True) -> Any:
        return self._kite.basket_order_margins(
            list(orders), consider_positions=consider_positions,
        )

    def positions(self) -> dict:
        return dict(self._kite.positions())
