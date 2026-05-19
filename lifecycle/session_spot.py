"""
lifecycle/session_spot.py
=========================

Build today's provisional OHLC bar for live trend (Zerodha OHLC + 5-min
spot snapshots fallback).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from database.connection import SQLServerConnection
from database.models import AtmIvTimeseriesRepo, SpotEodRepo
logger = logging.getLogger(__name__)


def _bar_from_zerodha(provider, symbol: str, trade_date: date) -> Optional[dict]:
    if not hasattr(provider, "get_index_day_ohlc"):
        return None
    try:
        row = provider.get_index_day_ohlc(symbol, trade_date)
        if row and float(row.get("close_price") or 0) > 0:
            return row
    except Exception:
        logger.debug("Zerodha day OHLC unavailable for %s", symbol, exc_info=True)
    return None


def _bar_from_snapshots(
    db: SQLServerConnection,
    symbol: str,
    trade_date: date,
    spot_now: float,
) -> Optional[dict]:
    """Synthesize session bar from 5-min ATM IV spot samples + prior EOD close."""
    since = datetime.combine(trade_date, datetime.min.time())
    rows = AtmIvTimeseriesRepo(db).recent_spot_for_symbol(symbol, since, limit=48)
    spots = [float(r["spot"]) for r in rows if r.get("spot")]
    sp = SpotEodRepo(db)
    prior = sp.for_date(symbol, trade_date)
    open_px = float(prior["close_price"]) if prior else (spots[0] if spots else spot_now)
    high_px = max([open_px, spot_now] + spots) if spots else max(open_px, spot_now)
    low_px = min([open_px, spot_now] + spots) if spots else min(open_px, spot_now)
    return {
        "trade_date": trade_date,
        "symbol": symbol,
        "open_price": open_px,
        "high_price": high_px,
        "low_price": low_px,
        "close_price": spot_now,
        "volume": 0,
    }


def build_session_bar(
    *,
    db: SQLServerConnection,
    symbol: str,
    trade_date: date,
    spot_now: float,
    chain_provider=None,
) -> Optional[dict]:
    """Provisional today bar for ``compute_trends`` in live mode."""
    if chain_provider is not None:
        bar = _bar_from_zerodha(chain_provider, symbol, trade_date)
        if bar:
            bar["close_price"] = spot_now
            if float(bar.get("high_price") or 0) < spot_now:
                bar["high_price"] = spot_now
            if float(bar.get("low_price") or 0) > spot_now:
                bar["low_price"] = spot_now
            return bar
    try:
        return _bar_from_snapshots(db, symbol, trade_date, spot_now)
    except Exception:
        logger.debug("snapshot session bar failed for %s", symbol, exc_info=True)
        prior = SpotEodRepo(db).for_date(symbol, trade_date)
        if not prior:
            return None
        open_px = float(prior["close_price"])
        return {
            "trade_date": trade_date,
            "symbol": symbol,
            "open_price": open_px,
            "high_price": max(open_px, spot_now),
            "low_price": min(open_px, spot_now),
            "close_price": spot_now,
            "volume": 0,
        }
