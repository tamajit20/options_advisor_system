"""
lifecycle/intraday_monitor.py
=============================

WebSocket-driven **entry-band** alerts for pending suggestions.

Subscribes to ``TOPIC_TICK`` and fires one notification type via ``Notifier``:

* **PERFECT_ENTRY** — every leg of a today-PENDING suggestion has a current
  LTP within its ``suggested_price_low / suggested_price_high`` band. One
  alert per suggestion per IST day.

Open-trade risk (loss limit, target, spot SL, short-leg stress) is handled
exclusively by ``lifecycle/live_risk_monitor.py``.

Design rules
------------
* **No DB writes per tick.** Snapshot reload default 60 s.
* **Read-only on Zerodha** — trade/suggestion state from our DB only.
* **Daily dedup** for entry alerts, reset at IST date boundary.
* **Fail-open** on reload errors.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK_OPTIONS, get_event_bus
from utils import now_ist


logger = logging.getLogger(__name__)


LegKey = Tuple[str, Optional[date], Optional[float], Optional[str]]


@dataclass(frozen=True)
class _SuggestionLegRef:
    suggestion_id: str
    trade_name: str
    leg_order: int
    action: str
    suggested_price: float
    suggested_price_low: float
    suggested_price_high: float
    key: LegKey


@dataclass
class _Snapshot:
    suggestions: Dict[str, List[_SuggestionLegRef]] = field(default_factory=dict)
    suggestion_index: Dict[LegKey, List[_SuggestionLegRef]] = field(default_factory=dict)


SnapshotLoader = Callable[[], _Snapshot]


def make_db_snapshot_loader(db) -> SnapshotLoader:
    """Load today-PENDING suggestions for entry-band monitoring."""

    def _load() -> _Snapshot:
        snap = _Snapshot()
        today = now_ist().date()
        sug_rows = db.fetch_all(
            "SELECT suggestion_id, trade_name FROM options_suggestions "
            "WHERE status = 'PENDING' AND entry_date = ?",
            [today],
        )
        for sr in sug_rows:
            sid = sr["suggestion_id"]
            trade_name = sr.get("trade_name") or sid
            leg_rows = db.fetch_all(
                "SELECT leg_order, symbol, expiry_date, strike, option_type, "
                "       action, suggested_price, suggested_price_low, "
                "       suggested_price_high "
                "FROM options_suggestion_legs WHERE suggestion_id = ? "
                "ORDER BY leg_order",
                [sid],
            )
            legs: List[_SuggestionLegRef] = []
            for lr in leg_rows:
                key = _to_leg_key(
                    symbol=lr["symbol"],
                    expiry=lr["expiry_date"],
                    strike=lr["strike"],
                    option_type=lr["option_type"],
                )
                legs.append(_SuggestionLegRef(
                    suggestion_id=sid,
                    trade_name=trade_name,
                    leg_order=int(lr["leg_order"]),
                    action=str(lr["action"]).upper(),
                    suggested_price=float(lr.get("suggested_price") or 0.0),
                    suggested_price_low=float(lr.get("suggested_price_low") or 0.0),
                    suggested_price_high=float(lr.get("suggested_price_high") or 0.0),
                    key=key,
                ))
            if legs:
                snap.suggestions[sid] = legs
                for ref in legs:
                    snap.suggestion_index.setdefault(ref.key, []).append(ref)
        return snap

    return _load


def _to_leg_key(*, symbol, expiry, strike, option_type) -> LegKey:
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    return (
        str(symbol),
        expiry,
        float(strike) if strike is not None else None,
        str(option_type).upper() if option_type else None,
    )


class IntradayMonitor:
    """Subscribes to TOPIC_TICK; dispatches PERFECT_ENTRY via Notifier."""

    def __init__(
        self,
        notifier,
        snapshot_loader: SnapshotLoader,
        *,
        event_bus: Optional[EventBus] = None,
        reload_interval_seconds: float = 60.0,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._notifier = notifier
        self._loader = snapshot_loader
        self._bus = event_bus or get_event_bus()
        self._reload_interval = float(reload_interval_seconds)
        self._clock = clock

        self._snap: _Snapshot = _Snapshot()
        self._last_reload_at: Optional[datetime] = None
        self._latest: Dict[LegKey, float] = {}
        self._dedup_date: date = self._clock().date()
        self._entry_alerted: Set[str] = set()

        self._lock = threading.RLock()
        self._unsub: Optional[Callable[[], None]] = None

    def start(self) -> None:
        with self._lock:
            if self._unsub is not None:
                return
            self._reload_locked()
            self._unsub = self._bus.subscribe(TOPIC_TICK_OPTIONS, self.on_tick)
        logger.info("IntradayMonitor: started (entry-band only)")

    def stop(self) -> None:
        with self._lock:
            if self._unsub is None:
                return
            try:
                self._unsub()
            finally:
                self._unsub = None
        logger.info("IntradayMonitor: stopped")

    def on_tick(self, quote: LiveQuote) -> None:
        try:
            if quote is None or quote.option_type is None:
                return

            key = (
                quote.symbol,
                quote.expiry,
                float(quote.strike) if quote.strike is not None else None,
                quote.option_type.upper() if quote.option_type else None,
            )
            ltp = float(quote.last_price or 0.0)
            if ltp <= 0:
                return

            with self._lock:
                self._latest[key] = ltp
                self._maybe_reload_locked()
                self._reset_dedup_if_new_day_locked()
                self._evaluate_pending_suggestions_locked(key)
        except Exception:
            logger.exception("IntradayMonitor.on_tick failed for %r", quote)

    def _maybe_reload_locked(self) -> None:
        now = self._clock()
        if (
            self._last_reload_at is None
            or (now - self._last_reload_at).total_seconds() >= self._reload_interval
        ):
            self._reload_locked()

    def _reload_locked(self) -> None:
        try:
            self._snap = self._loader() or _Snapshot()
        except Exception:
            logger.exception("IntradayMonitor: snapshot reload failed; keeping previous")
        self._last_reload_at = self._clock()

    def _reset_dedup_if_new_day_locked(self) -> None:
        today = self._clock().date()
        if today != self._dedup_date:
            self._dedup_date = today
            self._entry_alerted.clear()

    def _evaluate_pending_suggestions_locked(self, key: LegKey) -> None:
        affected_sids = {
            r.suggestion_id for r in self._snap.suggestion_index.get(key, ())
        }
        for sid in affected_sids:
            if sid in self._entry_alerted:
                continue
            if self._is_perfect_entry(sid):
                self._entry_alerted.add(sid)
                self._fire_perfect_entry(sid)

    def _is_perfect_entry(self, suggestion_id: str) -> bool:
        legs = self._snap.suggestions.get(suggestion_id) or []
        if not legs:
            return False
        for ref in legs:
            ltp = self._latest.get(ref.key)
            if ltp is None:
                return False
            lo = ref.suggested_price_low
            hi = ref.suggested_price_high
            if lo > 0 and ltp < lo:
                return False
            if hi > 0 and ltp > hi:
                return False
        return True

    def _fire_perfect_entry(self, suggestion_id: str) -> None:
        legs = self._snap.suggestions.get(suggestion_id) or []
        trade_name = legs[0].trade_name if legs else suggestion_id
        body = (
            "Every leg is now within its suggested price band — "
            "this is the recommended entry window."
        )
        try:
            self._notifier.notify(
                "PERFECT_ENTRY", "INFO",
                f"{trade_name}: perfect entry", body,
                related_suggestion_id=suggestion_id,
            )
        except Exception:
            logger.exception("IntradayMonitor: PERFECT_ENTRY dispatch failed")
