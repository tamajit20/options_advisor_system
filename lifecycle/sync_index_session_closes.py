"""
lifecycle/sync_index_session_closes.py
======================================

Upsert today's index / VIX session OHLC from Kite into EOD tables so the
header strip does not keep showing yesterday after the WS event ring
prunes post-close.

Safe to re-run (MERGE). Requires a valid Zerodha session.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from config import STRATEGY_CONFIG, ZERODHA_API_CONFIG
from contracts import SpotBhavRow, VixRow
from database.connection import SQLServerConnection
from database.models import SpotEodRepo, VixRepo
from providers.zerodha.session import is_token_valid, load_session
from utils import today_ist

logger = logging.getLogger(__name__)

_INDEX_KEYS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "VIX": "NSE:INDIA VIX",
}


def sync_index_session_closes(
    db: SQLServerConnection,
    *,
    trade_date: Optional[date] = None,
) -> dict:
    """Pull Kite OHLC for configured indexes + VIX and upsert EOD rows.

    Returns ``{"upserted": n, "symbols": {...}}``.
    """
    from kiteconnect import KiteConnect

    td = trade_date or today_ist()
    session = load_session()
    if session is None or not is_token_valid(session):
        raise RuntimeError("Zerodha session missing or expired — cannot sync indexes")

    api_key = (
        ZERODHA_API_CONFIG.get("api_key")
        or getattr(session, "api_key", None)
        or ""
    )
    if not api_key:
        raise RuntimeError("Zerodha API key missing")

    want = [s for s in (STRATEGY_CONFIG.get("underlyings") or []) if s in _INDEX_KEYS]
    if "VIX" not in want:
        want = list(want) + ["VIX"]
    keys = [_INDEX_KEYS[s] for s in want if s in _INDEX_KEYS]

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(session.access_token)
    payload = kite.ohlc(keys) or {}

    spot_rows: list[SpotBhavRow] = []
    vix_rows: list[VixRow] = []
    detail: dict[str, float] = {}

    for sym in want:
        kite_key = _INDEX_KEYS.get(sym)
        if not kite_key:
            continue
        row = payload.get(kite_key) or {}
        ohlc = row.get("ohlc") or {}
        try:
            close = float(row.get("last_price") if row.get("last_price") is not None
                          else ohlc.get("close"))
            open_px = float(ohlc.get("open") if ohlc.get("open") is not None else close)
            high_px = float(ohlc.get("high") if ohlc.get("high") is not None else close)
            low_px = float(ohlc.get("low") if ohlc.get("low") is not None else close)
        except (TypeError, ValueError):
            logger.warning("sync_index_session_closes: bad OHLC for %s: %s", sym, row)
            continue
        if close <= 0:
            continue
        detail[sym] = close
        if sym == "VIX":
            vix_rows.append(VixRow(
                trade_date=td,
                open_price=open_px,
                high_price=high_px,
                low_price=low_px,
                close_price=close,
            ))
        else:
            spot_rows.append(SpotBhavRow(
                trade_date=td,
                symbol=sym,
                open_price=open_px,
                high_price=high_px,
                low_price=low_px,
                close_price=close,
                volume=0,
            ))

    n = 0
    if spot_rows:
        n += SpotEodRepo(db).upsert_many(spot_rows)
    if vix_rows:
        n += VixRepo(db).upsert_many(vix_rows)
    db.commit()
    logger.info(
        "sync_index_session_closes: upserted %d row(s) for %s — %s",
        n, td.isoformat(), detail,
    )
    return {"upserted": n, "trade_date": td.isoformat(), "symbols": detail}
