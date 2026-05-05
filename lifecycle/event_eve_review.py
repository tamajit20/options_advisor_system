"""
lifecycle/event_eve_review.py
=============================

Phase 3 — #5: pre-emptive review reminder on the eve of HIGH-impact
events. Runs every weekday afternoon (default 14:30 IST). For each
ACTIVE trade, if the calendar has a HIGH-impact event for **tomorrow**,
fire an `EVENT_AHEAD_REVIEW` notification so the operator can review
the position with extra time (vs. the existing day-of squeeze).

This is purely informational — it does not change any trade state or
generate exit decisions. The dashboard surfaces the notification and
the user decides whether to silence alerts (`silence_alerts_until`),
hedge, or close early.

Idempotent: the scheduler runs this once per weekday. We fire one
notification per trade per scheduler run; duplicate suppression is
handled at the notification-rendering layer (and by the user).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from contracts import Notification
from database.connection import SQLServerConnection
from database.models import EventCalendarRepo, NotificationRepo, TradeRepo
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)


def run_event_eve_review(
    db: SQLServerConnection,
    *,
    today: Optional[date] = None,
) -> int:
    """Insert one EVENT_AHEAD_REVIEW notification per ACTIVE trade if
    tomorrow has a HIGH-impact event. Returns the count inserted."""
    today = today or today_ist()
    tomorrow = today + timedelta(days=1)

    cal = EventCalendarRepo(db)
    if not cal.has_high_impact(tomorrow, tomorrow):
        logger.info("event_eve_review: no HIGH-impact events on %s — skipping",
                    tomorrow)
        return 0

    ev = cal.first_high_impact_event(tomorrow, tomorrow) or {}
    ev_label = ev.get("description") or ev.get("event_type") or "HIGH-impact event"

    trd = TradeRepo(db)
    notif = NotificationRepo(db)
    inserted = 0
    for trade in trd.open_trades():
        status = (trade.get("status") or "").upper()
        if status != "ACTIVE":
            continue
        trade_id = trade["trade_id"]
        title = (
            f"{trade.get('trade_name') or trade_id}: review before "
            f"{ev_label} tomorrow"
        )
        body = (
            f"A HIGH-impact event ({ev_label}) is scheduled for "
            f"{tomorrow.isoformat()}. Consider reducing size, hedging, "
            f"or closing this position before the event window. "
            f"Use 'Silence alerts' if you choose to ride through."
        )
        try:
            notif.insert(Notification(
                created_at=now_ist(),
                notif_type="EVENT_AHEAD_REVIEW",
                severity="WARNING",
                title=title,
                body=body,
                related_trade_id=trade_id,
            ))
            inserted += 1
        except Exception as exc:  # pragma: no cover - DB best-effort
            logger.exception(
                "event_eve_review: failed to insert notification for %s: %s",
                trade_id, exc,
            )

    if inserted:
        db.commit()
    logger.info(
        "event_eve_review: %d EVENT_AHEAD_REVIEW notifications inserted "
        "for events on %s", inserted, tomorrow,
    )
    return inserted
