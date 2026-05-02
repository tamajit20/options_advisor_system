"""
lifecycle/events_seeder.py
==========================

Populates options_events_calendar with HIGH-impact market events.

Two sources, used in order of preference:
  1. Trading Economics API (auto, real-time)
       Fetches the next 60 days for India + US whenever OPT_TE_API_KEY is set.
       Free API key: https://tradingeconomics.com/api/
  2. EVENTS_CONFIG in config.py (static fallback)
       Manual list — used when no API key is configured.
       Also always applied so known far-future events (e.g. next Budget)
       are covered even if the API window hasn't reached them yet.

Idempotent: rows with the same (event_date, event_type) are never
inserted twice, so the job can run weekly safely.

Returns the count of newly inserted rows.
"""

from __future__ import annotations

import logging
import os
from datetime import date

from config import EVENTS_CONFIG
from database.connection import SQLServerConnection

logger = logging.getLogger(__name__)


def _insert_if_new(db: SQLServerConnection, ev_date: str, ev_type: str,
                   desc: str, impact: str) -> bool:
    """Insert a single event row if it doesn't already exist. Returns True if inserted."""
    exists = db.scalar(
        "SELECT COUNT(*) FROM options_events_calendar "
        "WHERE event_date = ? AND event_type = ?",
        [ev_date, ev_type],
    )
    if exists:
        return False
    db.execute(
        "INSERT INTO options_events_calendar (event_date, event_type, description, impact) "
        "VALUES (?, ?, ?, ?)",
        [ev_date, ev_type, desc, impact],
    )
    logger.info("Events calendar: inserted %s %s — %s", ev_date, ev_type, desc)
    return True


def run_events_seed(db: SQLServerConnection, _trade_date: date | None = None) -> int:
    """Seed options_events_calendar from API (if key set) + static EVENTS_CONFIG.

    `_trade_date` is accepted (but ignored) so this fits the scheduler's
    standard ``fn(db, trade_date) -> int`` signature.
    """
    inserted = 0

    # ── Source 1: Trading Economics API (live, automatic) ──────────────────
    api_key = os.environ.get("OPT_TE_API_KEY", "").strip()
    if api_key:
        try:
            from downloader.economic_calendar import fetch_high_impact_events
            api_events = fetch_high_impact_events(api_key)
            for ev in api_events:
                if _insert_if_new(db, ev["date"], ev["event_type"],
                                  ev["description"], ev.get("impact", "HIGH")):
                    inserted += 1
        except Exception:
            logger.exception("Events seeder: API fetch failed (falling through to static config)")
    else:
        logger.info("Events seeder: OPT_TE_API_KEY not set — using static EVENTS_CONFIG only. "
                    "Register free at https://tradingeconomics.com/api/ to enable auto-fetch.")

    # ── Source 2: static EVENTS_CONFIG (fallback + far-future coverage) ────
    for ev in EVENTS_CONFIG:
        if _insert_if_new(db, ev["date"], ev["event_type"],
                          ev.get("description", ""), ev.get("impact", "HIGH")):
            inserted += 1

    if inserted:
        db.commit()
        logger.info("Events seeder complete: %d new rows inserted", inserted)
    else:
        logger.debug("Events seeder: nothing new to insert")

    return inserted
