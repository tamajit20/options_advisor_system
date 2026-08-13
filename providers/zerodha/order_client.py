"""
providers/zerodha/order_client.py
=================================

Explicit Kite Connect order API wrapper for Scout live execution.

Kept separate from the read-only ``KiteFacade`` so market-data paths cannot
accidentally place orders. Only ``scout.execution_engine`` should import this
module, and only when persisted scout settings have ``zerodha_execute_orders`` enabled.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from config import ZERODHA_API_CONFIG
from providers.zerodha.session import is_token_valid, load_session

logger = logging.getLogger(__name__)


class ZerodhaOrderError(Exception):
    """Order placement or broker communication failed."""

_last_order_error: Optional[str] = None


def last_order_error() -> Optional[str]:
    return _last_order_error


def _record_order_error(exc: Exception) -> None:
    global _last_order_error
    _last_order_error = str(exc)


class KiteOrderClient:
    """Minimal write-side Kite client for Scout MIS equity orders."""

    def __init__(self, kite_client: Optional[Any] = None):
        if kite_client is not None:
            self._kite = kite_client
            return
        sess = load_session()
        if not sess or not sess.access_token:
            raise ZerodhaOrderError("Not logged in to Zerodha")
        if not is_token_valid(sess):
            raise ZerodhaOrderError("Zerodha token expired — log in again")
        api_key = ZERODHA_API_CONFIG.get("api_key") or sess.api_key
        if not api_key:
            raise ZerodhaOrderError("Zerodha API key not configured")
        try:
            from kiteconnect import KiteConnect  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ZerodhaOrderError(
                "kiteconnect SDK not installed; pip install kiteconnect>=5.2"
            ) from exc
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(sess.access_token)
        self._kite = kite

    def place_order(self, **params) -> str:
        try:
            order_id = self._kite.place_order(variety="regular", **params)
            logger.info(
                "Kite place_order %s %s qty=%s → order_id=%s",
                params.get("exchange"), params.get("tradingsymbol"),
                params.get("quantity"), order_id,
            )
            return str(order_id)
        except Exception as exc:
            _record_order_error(exc)
            raise ZerodhaOrderError(str(exc)) from exc

    def modify_order(self, order_id: str, **params) -> str:
        try:
            out = self._kite.modify_order(
                variety="regular", order_id=str(order_id), **params,
            )
            logger.info("Kite modify_order order_id=%s params=%s", order_id, params)
            return str(out)
        except Exception as exc:
            _record_order_error(exc)
            raise ZerodhaOrderError(str(exc)) from exc

    def cancel_order(self, order_id: str) -> str:
        try:
            out = self._kite.cancel_order(variety="regular", order_id=str(order_id))
            logger.info("Kite cancel_order order_id=%s", order_id)
            return str(out)
        except Exception as exc:
            _record_order_error(exc)
            raise ZerodhaOrderError(str(exc)) from exc

    def order_history(self, order_id: str) -> List[dict]:
        try:
            return list(self._kite.order_history(str(order_id)))
        except Exception as exc:
            _record_order_error(exc)
            raise ZerodhaOrderError(str(exc)) from exc

    def margins(self, segment: Optional[str] = None) -> dict:
        try:
            if segment:
                return dict(self._kite.margins(segment=segment))
            return dict(self._kite.margins())
        except Exception as exc:
            _record_order_error(exc)
            raise ZerodhaOrderError(str(exc)) from exc

    def order_margins(self, orders: List[dict]) -> list:
        try:
            return list(self._kite.order_margins(orders))
        except Exception as exc:
            _record_order_error(exc)
            raise ZerodhaOrderError(str(exc)) from exc

    @staticmethod
    def latest_status(history: List[dict]) -> Dict[str, Any]:
        if not history:
            return {"status": "UNKNOWN"}
        last = history[-1]
        return {
            "status": str(last.get("status") or "UNKNOWN").upper(),
            "average_price": last.get("average_price"),
            "filled_quantity": last.get("filled_quantity"),
            "status_message": last.get("status_message"),
            "exchange_order_id": last.get("exchange_order_id"),
        }
