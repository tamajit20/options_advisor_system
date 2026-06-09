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
from contracts import SpotBhavRow, VixRow
from database.connection import SQLServerConnection
from database.models import ExpiryCalendarRepo, FiiRepo, FoEodRepo, SpotEodRepo, VixRepo
from downloader.fii_data import download_fii_oi
from downloader.fo_bhav import download_fo_bhav, extract_index_spots
from downloader.index_spot_nse import download_nse_index_spot
from downloader.spot_bhav import download_spot_bhav
from exceptions import NoDataError
from lifecycle.spot_bhav_merge import merge_spot_bhav_rows
from downloader.vix import download_vix_for_date, download_vix_history, load_bundled_vix_rows
from utils import today_ist

logger = logging.getLogger(__name__)


def run_fo_bhav(db: SQLServerConnection, trade_date: date | None = None) -> int:
    trade_date = trade_date or today_ist()
    rows = download_fo_bhav(trade_date)
    if not rows:
        raise NoDataError(
            f"FO bhavcopy not available for {trade_date} — "
            "market holiday or NSE has not published the file yet"
        )
    n = FoEodRepo(db).upsert_many(rows)
    try:
        added = ExpiryCalendarRepo(db).upsert_from_fo_rows(rows)
        if added:
            logger.info("Expiry calendar: refreshed %d (symbol, expiry) pairs", added)
    except Exception as exc:
        logger.warning("Expiry calendar refresh failed (non-fatal): %s", exc)
    db.commit()
    logger.info("FO bhav %s: upserted %d rows", trade_date, n)

    # Settle-time hook for review item #10 — record realised vs expected
    # moves for every suggestion that just expired.  Best-effort: a
    # failure here must NOT roll back the bhav upsert above.
    try:
        from lifecycle.em_calibration_recorder import record_settled_expiries
        recorded = record_settled_expiries(db, trade_date)
        if recorded:
            db.commit()
    except Exception:
        logger.exception("EM-calib recorder failed (non-fatal)")
        try:
            db.rollback()
        except Exception:
            pass
    return n


def run_spot_bhav(db: SQLServerConnection, trade_date: date | None = None) -> int:
    """Cash bhav for stocks; NSE ``ind_close_all`` for index OHLC; F&O
    ``UndrlygPric`` only when index OHLC is unavailable (never overwrites
    real high/low)."""
    trade_date = trade_date or today_ist()
    stock_rows = list(download_spot_bhav(trade_date))

    indices = [u for u in STRATEGY_CONFIG["underlyings"] if u in {
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "BANKEX", "SENSEX",
    }]
    index_rows: list[SpotBhavRow] = []
    if indices:
        try:
            index_rows = download_nse_index_spot(trade_date, keep_only=indices)
        except Exception as exc:
            logger.warning("NSE index close download failed: %s", exc)
        if index_rows:
            logger.info(
                "NSE index close %s: %d rows (%s)",
                trade_date, len(index_rows),
                ", ".join(r.symbol for r in index_rows),
            )

    fo_settle: dict[str, float] = {}
    if indices:
        try:
            fo_settle = extract_index_spots(trade_date, indices)
        except Exception as exc:
            logger.warning("Could not derive index spot from F&O bhav: %s", exc)
        if fo_settle:
            logger.info("F&O settle fallback available for: %s", ", ".join(fo_settle))

    rows = merge_spot_bhav_rows(stock_rows, index_rows, fo_settle, trade_date)

    if not rows:
        raise NoDataError(
            f"Spot bhavcopy not available for {trade_date} — "
            "market holiday or NSE has not published the file yet"
        )
    n = SpotEodRepo(db).upsert_many(rows)
    db.commit()
    logger.info("Spot bhav %s: upserted %d rows", trade_date, n)
    return n


def _seed_vix_from_bundled_csv(db: SQLServerConnection) -> int:
    """Seed options_vix_history from the bundled historical VIX CSV when the
    table has fewer than 30 rows (cold-start or fresh DB)."""
    rows = load_bundled_vix_rows()
    if not rows:
        return 0
    n = VixRepo(db).upsert_many(rows)
    db.commit()
    logger.info("VIX seed: loaded %d rows from bundled CSV", n)
    return n


def run_vix(db: SQLServerConnection, trade_date: date | None = None) -> int:
    # Auto-seed from bundled CSV when table is nearly empty (cold start / fresh DB)
    vix_repo = VixRepo(db)
    if trade_date is None and vix_repo.count() < 30:
        logger.info("VIX table has < 30 rows — seeding from bundled historical CSV")
        _seed_vix_from_bundled_csv(db)

    if trade_date is not None:
        rows = download_vix_for_date(trade_date)
        if not rows:
            raise NoDataError(
                f"VIX data not available for {trade_date} — "
                "not in bundled history and NSE live/archive had no match"
            )
    else:
        rows = download_vix_history()
        if not rows:
            raise NoDataError(
                "VIX history download returned no rows — "
                "NSE may not have published today's VIX data yet"
            )
    n = vix_repo.upsert_many(rows)
    db.commit()
    logger.info("VIX %s: upserted %d rows", trade_date or "latest", n)
    return n


def run_fii(db: SQLServerConnection, trade_date: date | None = None) -> int:
    trade_date = trade_date or today_ist()
    rows = download_fii_oi(trade_date)
    if not rows:
        raise NoDataError(
            f"FII OI data not available for {trade_date} — "
            "market holiday or SEBI/NSE has not published the file yet"
        )
    n = FiiRepo(db).upsert_many(rows)
    db.commit()
    logger.info("FII OI %s: upserted %d rows", trade_date, n)
    return n
