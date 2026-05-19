"""
lifecycle/index_spot_backfill.py
================================

Backfill ``options_spot_eod`` for index underlyings using NSE archives and/or
Zerodha historical candles.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List, Optional

from config import STRATEGY_CONFIG
from contracts import SpotBhavRow
from database.connection import SQLServerConnection
from database.models import SpotEodRepo
from downloader.index_spot_nse import download_nse_index_spot
from downloader.index_spot_zerodha import backfill_underlyings
from utils import today_ist

logger = logging.getLogger(__name__)

_INDEX_SYMBOLS = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "BANKEX", "SENSEX",
})


def _configured_indices() -> List[str]:
    return [s for s in STRATEGY_CONFIG["underlyings"] if s in _INDEX_SYMBOLS]


def run_index_spot_backfill(
    db: SQLServerConnection,
    *,
    days: Optional[int] = None,
    end_date: Optional[date] = None,
    use_zerodha: bool = True,
    use_nse: bool = True,
) -> int:
    """Fill index OHLC for the last ``days`` calendar days. Returns rows upserted."""
    n_days = days if days is not None else int(STRATEGY_CONFIG.get("index_spot_backfill_days", 400))
    end = end_date or today_ist()
    start = end - timedelta(days=n_days)
    symbols = _configured_indices()
    if not symbols:
        return 0

    repo = SpotEodRepo(db)
    total = 0

    if use_zerodha:
        try:
            batches = backfill_underlyings(start, end, symbols=symbols)
            for sym, rows in batches.items():
                if rows:
                    total += repo.upsert_many(rows)
            db.commit()
            logger.info("Index backfill (Zerodha): upserted %d rows", total)
        except Exception:
            logger.exception("Zerodha index backfill failed (non-fatal)")
            db.rollback()

    if use_nse:
        d = start
        nse_count = 0
        while d <= end:
            if d.weekday() < 5:
                try:
                    rows = download_nse_index_spot(d, keep_only=set(symbols))
                    if rows:
                        nse_count += repo.upsert_many(rows)
                except Exception:
                    logger.debug("NSE index %s failed", d, exc_info=True)
            d += timedelta(days=1)
        db.commit()
        total += nse_count
        logger.info("Index backfill (NSE): upserted %d rows", nse_count)

    return total
