"""
scout/market_data.py — Zerodha read-only access for scout (shared session file).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config import SCOUT_CONFIG, ZERODHA_API_CONFIG
from providers.zerodha.facade import KiteFacade
from providers.zerodha.session import is_token_valid, load_session

from scout.candles import bars_from_kite
from scout.candles import Candle
from scout.utils import session_start_dt

logger = logging.getLogger(__name__)

_init_lock = threading.Lock()
_token_map: Dict[str, int] = {}
_benchmark_key = "NSE:NIFTY 50"


class ScoutMarketError(Exception):
    pass


def zerodha_ready() -> Tuple[bool, str]:
    sess = load_session()
    if not sess or not sess.access_token:
        return False, "Not logged in — use the 🔑 button (same login as Options Advisor)"
    if not is_token_valid(sess):
        return False, "Zerodha token expired — log in again"
    return True, "Connected"


class ScoutMarketData:
    """Minute bars + session OHLC for scout watchlist symbols."""

    def __init__(self):
        ok, msg = zerodha_ready()
        if not ok:
            raise ScoutMarketError(msg)
        sess = load_session()
        assert sess and sess.access_token
        self._facade = KiteFacade(
            api_key=ZERODHA_API_CONFIG["api_key"],
            access_token=sess.access_token,
        )

    def _ensure_tokens(self) -> None:
        global _token_map
        if _token_map:
            return
        with _init_lock:
            if _token_map:
                return
            rows = self._facade.instruments("NSE")
            for r in rows:
                sym = r.get("tradingsymbol")
                if sym:
                    _token_map[sym] = int(r["instrument_token"])
            logger.info("Scout instrument map loaded (%d NSE symbols)", len(_token_map))

    def _token(self, symbol: str) -> int:
        self._ensure_tokens()
        tok = _token_map.get(symbol)
        if not tok:
            raise ScoutMarketError(f"Unknown NSE symbol: {symbol}")
        return tok

    def benchmark_pct_from_open(self) -> float:
        try:
            q = self._facade.ohlc([_benchmark_key])
            row = q.get(_benchmark_key) or {}
            o = float(row.get("ohlc", {}).get("open") or 0)
            ltp = float(row.get("last_price") or 0)
            if o <= 0:
                return 0.0
            return (ltp - o) / o * 100.0
        except Exception as exc:
            logger.warning("Scout benchmark quote failed: %s", exc)
            return 0.0

    def minute_bars(self, symbol: str) -> Tuple[List[Candle], dict]:
        """Return today's 1m candles + session stats."""
        token = self._token(symbol)
        start = session_start_dt()
        now = datetime.now()
        try:
            raw = self._facade.historical_data(
                token, start, now, "minute", oi=False,
            )
        except Exception as exc:
            raise ScoutMarketError(f"{symbol}: minute history failed — {exc}") from exc
        candles = bars_from_kite(raw)
        key = f"NSE:{symbol}"
        try:
            ohlc = self._facade.ohlc([key]).get(key) or {}
        except Exception:
            ohlc = {}
        o_row = ohlc.get("ohlc") or {}
        stats = {
            "open": float(o_row.get("open") or (candles[0].open if candles else 0)),
            "high": float(o_row.get("high") or 0),
            "low": float(o_row.get("low") or 0),
            "ltp": float(ohlc.get("last_price") or (candles[-1].close if candles else 0)),
        }
        if stats["high"] <= 0 and candles:
            stats["high"] = max(c.high for c in candles)
        if stats["low"] <= 0 and candles:
            stats["low"] = min(c.low for c in candles)
        return candles, stats

    @staticmethod
    def watchlist() -> List[str]:
        return list(SCOUT_CONFIG.get("watchlist") or [])
