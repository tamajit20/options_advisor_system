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

import csv
import logging
import os
from datetime import date, datetime

from config import STRATEGY_CONFIG
from contracts import SpotBhavRow, VixRow
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


def _seed_vix_from_bundled_csv(db: SQLServerConnection) -> int:
    """Seed options_vix_history from the bundled historical VIX CSV when the
    table has fewer than 30 rows (cold-start or fresh DB).  The file lives at
    downloader/hist_india_vix_-30-04-2025-to-30-04-2026.csv.

    CSV format: Date, Open, High, Low, Close, Prev. Close, Change, % Change
    Date format: 30-APR-2025  (dd-MMM-yyyy, uppercase month abbreviation)
    """
    csv_path = os.path.join(
        os.path.dirname(__file__),
        "..", "downloader",
        "hist_india_vix_-30-04-2025-to-30-04-2026.csv",
    )
    csv_path = os.path.normpath(csv_path)
    if not os.path.exists(csv_path):
        logger.warning("VIX seed: bundled CSV not found at %s", csv_path)
        return 0

    rows: list[VixRow] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for rec in reader:
            raw_date = rec.get("Date", "").strip()
            if not raw_date:
                continue
            try:
                dt = datetime.strptime(raw_date, "%d-%b-%Y").date()
            except ValueError:
                try:
                    dt = datetime.strptime(raw_date, "%d-%m-%Y").date()
                except ValueError:
                    logger.debug("VIX seed: unparseable date %r — skipping", raw_date)
                    continue
            try:
                rows.append(VixRow(
                    trade_date=dt,
                    open_price=float(rec.get("Open", "0").replace(",", "") or 0),
                    high_price=float(rec.get("High", "0").replace(",", "") or 0),
                    low_price=float(rec.get("Low", "0").replace(",", "") or 0),
                    close_price=float(rec.get("Close", "0").replace(",", "") or 0),
                ))
            except (ValueError, KeyError) as exc:
                logger.debug("VIX seed: skipping row %r: %s", raw_date, exc)

    if not rows:
        return 0
    n = VixRepo(db).upsert_many(rows)
    db.commit()
    logger.info("VIX seed: loaded %d rows from bundled CSV", n)
    return n


def run_vix(db: SQLServerConnection) -> int:
    # Auto-seed from bundled CSV when table is nearly empty (cold start / fresh DB)
    vix_repo = VixRepo(db)
    if vix_repo.count() < 30:
        logger.info("VIX table has < 30 rows — seeding from bundled historical CSV")
        _seed_vix_from_bundled_csv(db)

    rows = download_vix_history()
    if not rows:
        logger.warning("VIX: no rows downloaded")
        return 0
    n = vix_repo.upsert_many(rows)
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
