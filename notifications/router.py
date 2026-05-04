"""
notifications/router.py
=======================

The `Notifier` is the single entry-point any module should call to raise an
event-of-interest. It does three things, in order:

    1. Persists the event to `options_notifications` (existing dashboard
       sink).
    2. Honours runtime-flag gates by category — e.g. SL_TRIGGER events are
       suppressed when `FLAG_SL_ALERTS` is OFF.
    3. Fans out to every outbound channel that `accepts()` the event.

A channel raising an exception is *not* fatal. The router logs and moves on.
This is critical: a flaky SMTP server or Telegram outage must not be able to
bring down the trade engine.

Category → flag mapping
-----------------------
The category is derived from `notif_type`:

    `SL_TRIGGER`        → FLAG_SL_ALERTS
    `PERFECT_CLOSURE`   → FLAG_CLOSURE_ALERTS
    `PERFECT_ENTRY`     → FLAG_OPPORTUNITY_ALERTS
    everything else     → no flag (always allowed)

Calls that bypass the runtime-flag gate (operator-driven actions, system
errors) pass `bypass_flags=True`.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence

from contracts import Notification
from utils import now_ist

from .channels import Channel


logger = logging.getLogger(__name__)


# Mapping from notif_type to the runtime flag that must be ON for the event
# to be dispatched. Keys are uppercase. A missing key means "no gate".
_TYPE_TO_FLAG = {
    "SL_TRIGGER":             "sl_alerts",
    "PERFECT_CLOSURE":        "closure_alerts",
    "PERFECT_ENTRY":          "opportunity_alerts",
    "OPPORTUNITY_REGEN_HINT": "opportunity_alerts",
    # 09:35 validator outputs are also gated by opportunity_alerts — they
    # advise the user whether to act on the morning suggestion. The flag is
    # the same toggle a user would flip when they want to silence regen +
    # validator chatter together.
    "SUGGESTION_STILL_GOOD":  "opportunity_alerts",
    "SUGGESTION_STALE":       "opportunity_alerts",
    # Adverse-move early warning fires before SL_HIT — share the same flag
    # so silencing SL chatter also silences the early warning (the user
    # already opted out of risk-side notifications).
    "ADVERSE_MOVE_WARNING":   "sl_alerts",
}


class Notifier:
    """Persist + dispatch events.

    Parameters
    ----------
    notif_repo:
        Anything exposing `insert(Notification)`. Production callers pass
        `database.models.NotificationRepo(db)`. Tests can pass a stub.
    channels:
        Iterable of channels. Order is preserved. May be empty (the event
        is then only persisted).
    flag_repo:
        Anything exposing `get_bool(key, default=...)`. Production callers
        pass `database.runtime_flags.RuntimeFlagsRepo(db)`. None disables
        flag gating entirely (every event is dispatched).
    """

    def __init__(
        self,
        notif_repo,
        channels: Optional[Iterable[Channel]] = None,
        *,
        flag_repo=None,
        provider: Optional[str] = None,
    ):
        self._repo = notif_repo
        self._channels: List[Channel] = list(channels or [])
        self._flag_repo = flag_repo
        # Provider tag stamped onto every Notification's provenance. Optional.
        self._provider = provider

    # ------------------------------------------------------------------ public
    def notify(
        self,
        notif_type: str,
        severity: str,
        title: str,
        body: str = "",
        *,
        related_suggestion_id: Optional[str] = None,
        related_trade_id: Optional[str] = None,
        bypass_flags: bool = False,
        source_event_id: Optional[str] = None,
        tick_age_ms: Optional[int] = None,
    ) -> Notification:
        """Persist + dispatch. Returns the constructed `Notification`.

        Persistence happens unconditionally so the dashboard always shows a
        record. Channel dispatch is gated by:
            * the per-type runtime flag (unless `bypass_flags=True`)
            * each channel's own `accepts()` (typically severity-floor)

        Provenance markers (Phase 2c)
        -----------------------------
        Every dispatched notification is stamped with:
            * `provider`               — set on this `Notifier` at construction
            * `flag_state_at_dispatch` — JSON snapshot of all runtime flags
            * `source_event_id`        — caller-supplied tick/cycle id
            * `tick_age_ms`            — caller-supplied age of the trigger tick
        Markers are best-effort: if the flag-repo snapshot can't be read,
        we proceed without it (fail-open).
        """
        notif = Notification(
            created_at=now_ist(),
            notif_type=notif_type,
            severity=severity,
            title=title,
            body=body,
            related_suggestion_id=related_suggestion_id,
            related_trade_id=related_trade_id,
            source_event_id=source_event_id,
            provider=self._provider,
            tick_age_ms=tick_age_ms,
            flag_state_at_dispatch=self._flag_snapshot(),
        )

        # 1. Persist to DB (best-effort; we still try channels if this fails).
        try:
            self._repo.insert(notif)
        except Exception as exc:
            logger.exception(
                "notifications: DB insert failed (%s): %s", notif_type, exc
            )

        # 2. Runtime-flag gate.
        if not bypass_flags and not self._flag_allows(notif_type):
            logger.debug(
                "notifications: %s suppressed by runtime flag", notif_type
            )
            return notif

        # 3. Fan out to channels. Each is isolated.
        for ch in self._channels:
            try:
                if ch.accepts(notif):
                    ch.send(notif)
            except Exception as exc:
                logger.warning(
                    "notifications: channel %r raised on %s: %s",
                    getattr(ch, "name", type(ch).__name__), notif_type, exc,
                )

        return notif

    # ------------------------------------------------------------------ helpers
    def _flag_allows(self, notif_type: str) -> bool:
        """True if the event's category flag is ON (or there is no flag).

        A missing flag-repo or any error reading the flag is treated as
        permissive (fail-open) — same posture as the WS subscription
        manager. We never want a flag-DB outage to silence the system.
        """
        flag = _TYPE_TO_FLAG.get(notif_type.upper())
        if flag is None or self._flag_repo is None:
            return True
        try:
            return bool(self._flag_repo.get_bool(flag, default=True))
        except Exception as exc:
            logger.warning(
                "notifications: cannot read flag %s (%s); defaulting to allow",
                flag, exc,
            )
            return True

    def _flag_snapshot(self) -> Optional[str]:
        """Return a compact JSON snapshot of every runtime flag at dispatch
        time. Used purely for forensics. Returns None if no flag-repo is
        wired or the read fails (fail-open)."""
        if self._flag_repo is None:
            return None
        try:
            rows = self._flag_repo.all()
        except Exception as exc:
            logger.debug("notifications: flag snapshot failed: %s", exc)
            return None
        try:
            import json
            return json.dumps(
                {r.key: r.value for r in rows},
                default=str, separators=(",", ":"),
            )
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------
def build_notifier(db, *, provider: Optional[str] = None) -> Notifier:
    """Build a fully-wired `Notifier` from a connected `SQLServerConnection`:
    persists via `NotificationRepo`, gates via `RuntimeFlagsRepo`, and fans
    out to whichever channels are enabled in `ALERTS_CONFIG`.

    `provider` is stamped onto every notification's provenance — pass
    `"zerodha"` from the WS runner so each alert is traceable to the
    market-data source that triggered it."""
    from database.models import NotificationRepo
    from database.runtime_flags import RuntimeFlagsRepo
    from .channels import build_default_channels

    return Notifier(
        notif_repo=NotificationRepo(db),
        channels=build_default_channels(),
        flag_repo=RuntimeFlagsRepo(db),
        provider=provider,
    )
