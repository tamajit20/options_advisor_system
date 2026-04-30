"""
lifecycle/download_orchestrator.py
==================================

Daily data-download orchestrator. Each function:
    1. Calls the downloader (pure I/O, no DB)
    2. Upserts rows via repo (caller commits)
    3. Returns rows_processed for job logging

Each function is callable independently by the scheduler.
"""

from __future__ import annotations

import logging
from datetime import date

from config import STRATEGY_CONFIG
from contracts import SpotBhavRow
from database.connection import SQLServerConnection
from database.models import ExpiryCalendarRepo, FiiRepo, FoEodRepo, SpotEodRepo, VixRepo
from downloader.fii_data import download_fii_oi
from downloader.fo_bhav import download_fo_bhav, extract_index_spots
from downloader.spot_bhav import download_spot_bhav
from downloader.vix import download_vix_history
from utils import today_ist

logger = logging.getLogger(__name__)


def run_fo_bhav(db: SQLServerConnection, trade_date: date | None = None) -> int:
    trade_date = trade_date or today_ist()
    rows = download_fo_bhav(trade_date)
    if not rows:
        logger.warning("FO bhav: no rows for %s", trade_date)
        return 0
    n = FoEodRepo(db).upsert_many(rows)
    try:
        added = ExpiryCalendarRepo(db).upsert_from_fo_rows(rows)
        if added:
            logger.info("Expiry calendar: refreshed %d (symbol, expiry) pairs", added)
    except Exception as exc:
        logger.warning("Expiry calendar refresh failed (non-fatal): %s", exc)
    db.commit()
    logger.info("FO bhav %s: upserted %d rows", trade_date, n)
    return n


def run_spot_bhav(db: SQLServerConnection, trade_date: date | None = None) -> int:
    """Cash-market bhav contains only stocks. Indices (NIFTY/BANKNIFTY/...)
    are derived from the F&O bhav `UndrlygPric` column for the same date,
    which carries the official spot reference NSE used to settle options."""
    trade_date = trade_date or today_ist()
    rows = list(download_spot_bhav(trade_date))

    # Supplement with index spot closes from F&O bhav UndrlygPric.
    indices = [u for u in STRATEGY_CONFIG["underlyings"] if u in {
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "BANKEX", "SENSEX",
    }]
    if indices:
        try:
            spots = extract_index_spots(trade_date, indices)
        except Exception as exc:
            logger.warning("Could not derive index spot from F&O bhav: %s", exc)
            spots = {}
        for sym, px in spots.items():
            rows.append(SpotBhavRow(
                trade_date=trade_date, symbol=sym,
                open_price=px, high_price=px, low_price=px,
                close_price=px, volume=0,
            ))
        if spots:
            logger.info("Derived %d index spot rows from F&O bhav: %s",
                        len(spots), ", ".join(spots))

    if not rows:
        logger.warning("Spot bhav: no rows for %s", trade_date)
        return 0
    n = SpotEodRepo(db).upsert_many(rows)
    db.commit()
    logger.info("Spot bhav %s: upserted %d rows", trade_date, n)
    return n


def run_vix(db: SQLServerConnection) -> int:
    rows = download_vix_history()
    if not rows:
        logger.warning("VIX: no rows downloaded")
        return 0
    n = VixRepo(db).upsert_many(rows)
    db.commit()
    logger.info("VIX: upserted %d rows", n)
    return n


def run_fii(db: SQLServerConnection, trade_date: date | None = None) -> int:
    trade_date = trade_date or today_ist()
    rows = download_fii_oi(trade_date)
    if not rows:
        logger.warning("FII OI: no rows for %s (graceful)", trade_date)
        return 0
    n = FiiRepo(db).upsert_many(rows)
    db.commit()
    logger.info("FII OI %s: upserted %d rows", trade_date, n)
    return n
