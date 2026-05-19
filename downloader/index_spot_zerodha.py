"""
downloader/index_spot_zerodha.py
================================

Backfill index daily OHLC via Kite ``historical_data`` (read-only).

Requires a valid Zerodha session. Used by ``run_index_spot_backfill`` when
NSE archives are missing or for bulk history beyond NSE retention.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List, Optional

from config import STRATEGY_CONFIG, ZERODHA_API_CONFIG
from contracts import SpotBhavRow
from providers.zerodha.facade import KiteFacade
from providers.zerodha.instruments import InstrumentMaster
from providers.zerodha.provider import _normalise_index_symbol
from providers.zerodha.session import is_token_valid, load_session
from providers.zerodha.rate_limiter import TokenBucket

logger = logging.getLogger(__name__)

_RL_HIST = TokenBucket(rate_per_sec=3.0)


def _index_tradingsymbol(symbol: str) -> str:
    return _normalise_index_symbol(symbol.upper())


def _resolve_token(master: InstrumentMaster, symbol: str) -> Optional[int]:
    ts = _index_tradingsymbol(symbol)
    inst = master.get_by_tradingsymbol("NSE", ts)
    if inst is None:
        logger.warning("Zerodha backfill: instrument not found for %s (%s)", symbol, ts)
        return None
    return inst.instrument_token


def download_zerodha_index_history(
    symbol: str,
    from_date: date,
    to_date: date,
    *,
    facade: Optional[KiteFacade] = None,
    master: Optional[InstrumentMaster] = None,
) -> List[SpotBhavRow]:
    """Daily OHLC candles for one index symbol between ``from_date`` and ``to_date``."""
    if not ZERODHA_API_CONFIG.get("enabled", True):
        return []
    session = load_session()
    if session is None or not is_token_valid(session):
        logger.warning("Zerodha index backfill: no valid session")
        return []

    f = facade or KiteFacade(
        api_key=ZERODHA_API_CONFIG.get("api_key", ""),
        access_token=session.access_token,
    )
    m = master
    if m is None:
        m = InstrumentMaster(loader=lambda: f.instruments())
        m.refresh()

    token = _resolve_token(m, symbol)
    if token is None:
        return []

    _RL_HIST.acquire()
    candles = f.historical_data(
        instrument_token=token,
        from_date=from_date,
        to_date=to_date,
        interval="day",
    )
    out: List[SpotBhavRow] = []
    for c in candles or []:
        try:
            td = c["date"]
            if hasattr(td, "date"):
                td = td.date()
            out.append(SpotBhavRow(
                trade_date=td,
                symbol=symbol.upper(),
                open_price=float(c["open"]),
                high_price=float(c["high"]),
                low_price=float(c["low"]),
                close_price=float(c["close"]),
                volume=int(c.get("volume") or 0),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("skip candle: %s (%s)", c, exc)
    return out


def backfill_underlyings(
    from_date: date,
    to_date: date,
    *,
    symbols: Optional[List[str]] = None,
) -> Dict[str, List[SpotBhavRow]]:
    """Backfill all configured index underlyings."""
    syms = symbols or [
        s for s in STRATEGY_CONFIG["underlyings"]
        if s in {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "BANKEX", "SENSEX"}
    ]
    result: Dict[str, List[SpotBhavRow]] = {}
    for sym in syms:
        try:
            rows = download_zerodha_index_history(sym, from_date, to_date)
            result[sym] = rows
            logger.info("Zerodha backfill %s: %d daily bars", sym, len(rows))
        except Exception:
            logger.exception("Zerodha backfill failed for %s", sym)
            result[sym] = []
    return result
