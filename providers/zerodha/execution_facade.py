"""
providers/zerodha/execution_facade.py
=====================================

Write-path facade for Kite Connect order APIs. Kept separate from the
read-only `KiteFacade` so market-data code cannot accidentally place orders.

Only constructed when Zerodha execution is explicitly enabled.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional


logger = logging.getLogger(__name__)


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
        order_id = self._kite.place_order(**kwargs)
        return str(order_id)

    def modify_order(self, *, order_id: str, **kwargs) -> str:
        return str(self._kite.modify_order(order_id=order_id, **kwargs))

    def cancel_order(self, *, order_id: str, variety: str = "regular") -> str:
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
