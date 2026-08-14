"""Scheduler entrypoint for Intraday Scout."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import ScoutScanLogRepo, ScoutSignalRepo
from scout.config_loader import get_scout_settings
from scout.market_data import ScoutMarketData, ScoutMarketError, zerodha_ready
from scout.scanner import scan_watchlist
from scout.settings_schema import merge_scout_settings
from scout.utils import is_market_open
from utils import now_ist

logger = logging.getLogger(__name__)


def run_scout_scan(db: SQLServerConnection) -> int:
    """Run one scout scan. Returns number of signals stored."""
    if not SCOUT_CONFIG.get("enabled", True):
        logger.info("Scout scan skipped — disabled in config")
        return 0
    if not is_market_open():
        logger.info("Scout scan skipped — market closed")
        return 0
    ok, msg = zerodha_ready()
    if not ok:
        logger.warning("Scout scan skipped — %s", msg)
        return 0

    scan_id = f"scout-{now_ist().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    started = now_ist()
    log_repo = ScoutScanLogRepo(db)
    sig_repo = ScoutSignalRepo(db)
    log_repo.start(scan_id, started)

    signals_found = 0
    symbols_scanned = 0
    err_msg = None
    try:
        mkt = ScoutMarketData()
        rows, symbols_scanned = scan_watchlist(mkt, db)
        triggered = now_ist()
        settings = merge_scout_settings(get_scout_settings(db))
        dedupe_mins = int(settings.get("push_dedupe_minutes", 60))
        dedupe_per_symbol = bool(settings.get("dedupe_per_symbol", False))
        since_at = triggered - timedelta(minutes=dedupe_mins)
        for row in rows:
            sym = str(row["symbol"]).upper()
            stype = str(row["signal_type"])
            if sig_repo.has_recent_duplicate(
                symbol=sym,
                signal_type=stype,
                since_at=since_at,
                dedupe_per_symbol=dedupe_per_symbol,
            ):
                continue
            try:
                sig_repo.insert(
                    scan_id=scan_id,
                    symbol=sym,
                    exchange="NSE",
                    action=row["action"],
                    signal_type=stype,
                    reason=row["reason"],
                    ltp=float(row["ltp"]),
                    invalidation=row.get("invalidation"),
                    strength=row.get("strength") or "WEAK",
                    triggered_at=triggered,
                    meta={
                        **{
                            k: row[k] for k in row
                            if k not in (
                                "symbol", "action", "signal_type", "reason",
                                "ltp", "invalidation", "strength",
                            )
                        },
                        "source": "scan",
                    },
                )
                db.commit()
                signals_found += 1
            except Exception:
                db.rollback()
                logger.exception("Scout scan insert failed for %s", sym)
        log_repo.finish(
            scan_id,
            status="SUCCESS",
            finished_at=now_ist(),
            symbols_scanned=symbols_scanned,
            signals_found=signals_found,
        )
        db.commit()
        logger.info(
            "Scout scan %s done — %d signals from %d symbols",
            scan_id, signals_found, symbols_scanned,
        )
        return signals_found
    except ScoutMarketError as exc:
        err_msg = str(exc)[:500]
        log_repo.finish(
            scan_id,
            status="FAILED",
            finished_at=now_ist(),
            symbols_scanned=symbols_scanned,
            signals_found=0,
            error_message=err_msg,
        )
        db.commit()
        logger.warning("Scout scan failed: %s", exc)
        return 0
    except Exception as exc:
        err_msg = str(exc)[:500]
        log_repo.finish(
            scan_id,
            status="FAILED",
            finished_at=now_ist(),
            symbols_scanned=symbols_scanned,
            signals_found=0,
            error_message=err_msg,
        )
        db.rollback()
        logger.exception("Scout scan error")
        return 0
