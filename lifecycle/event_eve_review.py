"""
lifecycle/event_eve_review.py
=============================

Eve-of HIGH-impact event reminders for open risk.

* Job runs **morning** of the prior day (default 09:05 IST) so warnings start
  with the session, not mid-afternoon.
* ACTIVE **short / credit** trades → ``PRE_EVENT_EXIT`` (CRITICAL).
* Other ACTIVE trades → ``EVENT_AHEAD_REVIEW`` (WARNING).

Dashboard marquee lists each HIGH event as a separate dismissible item
(one line at a time). Purely advisory — no auto-close.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from contracts import Notification
from database.connection import SQLServerConnection
from database.models import (
    EventCalendarRepo,
    NotificationRepo,
    SuggestionRepo,
    TradeRepo,
)
from engine.strategy_selector import _CREDIT_STRATEGIES
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)

_SHORT_PREMIUM_STRATEGIES = frozenset(_CREDIT_STRATEGIES) | frozenset({
    "SHORT_STRANGLE",
    "SHORT_STRADDLE",
})


def is_short_premium_trade(
    *,
    strategy: Optional[str],
    strategy_type: Optional[str] = None,
    net_credit_actual: Optional[float] = None,
) -> bool:
    """True for credit / short-premium structures (gap risk into events)."""
    strat = str(strategy or "").upper()
    if strat in _SHORT_PREMIUM_STRATEGIES:
        return True
    if str(strategy_type or "").upper() == "WRITING":
        return True
    try:
        if net_credit_actual is not None and float(net_credit_actual) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def event_key(ev: dict) -> str:
    """Stable id for dismiss / UI (date|type|description)."""
    d = ev.get("event_date")
    if hasattr(d, "isoformat"):
        d_s = d.isoformat()
    else:
        d_s = str(d or "")[:10]
    et = str(ev.get("event_type") or "").strip()
    desc = str(ev.get("description") or "").strip()
    return f"{d_s}|{et}|{desc}"


def _strategy_for_trade(db: SQLServerConnection, trade: dict) -> Dict[str, Any]:
    sid = trade.get("suggestion_id")
    if not sid:
        return {}
    row = SuggestionRepo(db).get(str(sid))
    return row or {}


def _already_sent_today(
    db: SQLServerConnection,
    *,
    trade_id: str,
    notif_type: str,
    day: date,
) -> bool:
    day_start = datetime.combine(day, datetime.min.time())
    n = db.scalar(
        """
        SELECT COUNT(1) FROM options_notifications
        WHERE related_trade_id = ?
          AND notif_type = ?
          AND created_at >= ?
        """,
        [trade_id, notif_type, day_start],
    )
    return bool(n and int(n) > 0)


def active_short_premium_trades(
    db: SQLServerConnection,
) -> List[Dict[str, Any]]:
    """ACTIVE trades that are short/credit premium (enriched with strategy)."""
    out: List[Dict[str, Any]] = []
    for trade in TradeRepo(db).open_trades():
        if str(trade.get("status") or "").upper() != "ACTIVE":
            continue
        sug = _strategy_for_trade(db, trade)
        strategy = sug.get("strategy") or trade.get("strategy")
        strategy_type = sug.get("strategy_type")
        if not is_short_premium_trade(
            strategy=strategy,
            strategy_type=strategy_type,
            net_credit_actual=trade.get("net_credit_actual"),
        ):
            continue
        enriched = dict(trade)
        enriched["strategy"] = strategy
        enriched["strategy_type"] = strategy_type
        out.append(enriched)
    return out


def _as_date(raw) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _item_for_event(
    ev: dict,
    *,
    today: date,
    shorts: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    event_day = _as_date(ev.get("event_date"))
    if event_day is None:
        return None
    tomorrow = today + timedelta(days=1)
    if event_day == tomorrow:
        when = "tomorrow"
        close_phrase = "Consider closing tonight before the event window."
    elif event_day == today:
        when = "today"
        close_phrase = (
            "Event day — consider closing short premium before further gap risk."
        )
    else:
        return None

    ev_label = ev.get("description") or ev.get("event_type") or "HIGH-impact event"
    names = [
        str(t.get("trade_name") or t.get("trade_id") or "?")
        for t in shorts
    ]
    text = (
        f"PRE-EVENT EXIT · {ev_label} {when} ({event_day.isoformat()}) — "
        f"{close_phrase} "
        f"Shorts: {', '.join(names)}. "
        f"Advisory only."
    )
    return {
        "event_key": event_key(ev),
        "event_date": event_day.isoformat(),
        "event_type": str(ev.get("event_type") or ""),
        "event_label": str(ev_label),
        "when": when,
        "trade_ids": [t.get("trade_id") for t in shorts if t.get("trade_id")],
        "trade_names": names,
        "message": text,
    }


def build_pre_event_exit_banner(
    db: SQLServerConnection,
    *,
    today: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Live banner payload: one dismissible item per HIGH event (today/tomorrow).

    Requires at least one ACTIVE short/credit trade. Independent of unread
    notifications. Client shows items one-at-a-time on a single line.
    """
    today = today or today_ist()
    tomorrow = today + timedelta(days=1)
    cal = EventCalendarRepo(db)
    events = cal.high_impact_events(today, tomorrow)
    if not events:
        return None

    shorts = active_short_premium_trades(db)
    if not shorts:
        return None

    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for ev in events:
        item = _item_for_event(ev, today=today, shorts=shorts)
        if item is None:
            continue
        key = item["event_key"]
        if key in seen:
            continue
        seen.add(key)
        items.append(item)

    if not items:
        return None
    return {
        "active": True,
        "items": items,
        # Back-compat for older clients: first undismissed-looking item.
        "message": items[0]["message"],
        "event_key": items[0]["event_key"],
    }


def run_event_eve_review(
    db: SQLServerConnection,
    *,
    today: Optional[date] = None,
) -> int:
    """Morning job: notify on ACTIVE trades when tomorrow has HIGH event(s).

    Short/credit → ``PRE_EVENT_EXIT`` (CRITICAL). Others → ``EVENT_AHEAD_REVIEW``.
    Skips if the same notif_type was already inserted for that trade today.
    """
    today = today or today_ist()
    tomorrow = today + timedelta(days=1)

    cal = EventCalendarRepo(db)
    events = cal.high_impact_events(tomorrow, tomorrow)
    if not events:
        logger.info(
            "event_eve_review: no HIGH-impact events on %s — skipping",
            tomorrow,
        )
        return 0

    labels = [
        str(e.get("description") or e.get("event_type") or "HIGH-impact event")
        for e in events
    ]
    ev_label = "; ".join(labels)

    trd = TradeRepo(db)
    notif = NotificationRepo(db)
    inserted = 0
    for trade in trd.open_trades():
        status = str(trade.get("status") or "").upper()
        if status != "ACTIVE":
            continue
        trade_id = trade["trade_id"]
        sug = _strategy_for_trade(db, trade)
        strategy = sug.get("strategy")
        short = is_short_premium_trade(
            strategy=strategy,
            strategy_type=sug.get("strategy_type"),
            net_credit_actual=trade.get("net_credit_actual"),
        )
        notif_type = "PRE_EVENT_EXIT" if short else "EVENT_AHEAD_REVIEW"
        if _already_sent_today(
            db, trade_id=trade_id, notif_type=notif_type, day=today,
        ):
            continue

        name = trade.get("trade_name") or trade_id
        if short:
            title = (
                f"{name}: PRE-EVENT EXIT — consider close tonight "
                f"before {ev_label}"
            )
            body = (
                f"HIGH-impact event(s) scheduled for {tomorrow.isoformat()}: "
                f"{ev_label}. This is a short/credit position — overnight gap "
                f"risk can breach the intraday SL before the exit engine runs. "
                f"Consider closing tonight (or reducing size / hedging). "
                f"Advisory only — dismiss the top banner per event, or use "
                f"Silence alerts if you choose to ride through."
            )
            severity = "CRITICAL"
        else:
            title = f"{name}: review before {ev_label} tomorrow"
            body = (
                f"HIGH-impact event(s) scheduled for {tomorrow.isoformat()}: "
                f"{ev_label}. Consider reducing size, hedging, or closing "
                f"before the event window. Use 'Silence alerts' if you choose "
                f"to ride through."
            )
            severity = "WARNING"

        try:
            notif.insert(Notification(
                created_at=now_ist(),
                notif_type=notif_type,
                severity=severity,
                title=title,
                body=body,
                related_trade_id=trade_id,
            ))
            inserted += 1
        except Exception as exc:  # pragma: no cover
            logger.exception(
                "event_eve_review: failed to insert %s for %s: %s",
                notif_type, trade_id, exc,
            )

    if inserted:
        db.commit()
    logger.info(
        "event_eve_review: %d notifications inserted for events on %s",
        inserted, tomorrow,
    )
    return inserted
