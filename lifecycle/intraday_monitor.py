"""
lifecycle/intraday_monitor.py
=============================

Phase 2b-iii — WebSocket-driven instant alerts.

The monitor subscribes to the in-process event bus (`TOPIC_TICK`, published
by `providers.zerodha.ws_runner`) and fires three notification types via the
`Notifier` from `notifications/`:

* **SL_TRIGGER** — a SHORT leg's live LTP has risen above
  `fill_price × intraday_sl_multiplier` (default 2.0). One alert per leg
  per IST day.
* **PERFECT_CLOSURE** — every executed SHORT leg of an ACTIVE trade is now
  trading at or below `fill_price × (1 − take_profit_fraction)` (i.e. the
  configured profit-capture target has been reached on every short leg
  simultaneously). One alert per trade per IST day.
* **PERFECT_ENTRY** — every leg of a today-PENDING suggestion has a current
  LTP within its `suggested_price_low / suggested_price_high` band. One
  alert per suggestion per IST day.

Design rules
------------
* **No DB writes per tick.** The state needed to evaluate a tick is loaded
  via a periodic reload (default 60 s) — same cadence as the WS
  subscription manager — and held in memory.
* **Read-only on Zerodha.** The monitor never reads positions/orders from
  Kite. Trade state comes from our own `options_trades` /
  `options_trade_legs` joined with `options_suggestion_legs`.
* **Per-leg caching.** The latest LTP for every key
  `(symbol, expiry, strike, option_type)` is kept in a dict; multi-leg
  decisions (PERFECT_CLOSURE / PERFECT_ENTRY) read from this dict so a
  single tick can complete the picture once all sibling legs have been
  seen at least once.
* **Daily dedup.** Three sets are reset at the IST date boundary.
* **Fail-open.** Reload errors are logged and skipped — never raised.
* **Persistence still happens.** All channel dispatch goes through
  `Notifier.notify(...)` which always inserts a row into
  `options_notifications` and respects runtime-flag gates.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from config import STRATEGY_CONFIG
from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK, get_event_bus
from utils import now_ist


logger = logging.getLogger(__name__)


# A canonical key for an option leg. `expiry` is a `date` (not datetime).
LegKey = Tuple[str, Optional[date], Optional[float], Optional[str]]


# ---------------------------------------------------------------------------
# In-memory views built from the database
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _TradeLegRef:
    trade_id: str
    trade_name: str
    strategy: str
    leg_order: int
    action: str            # "SELL" | "BUY"
    fill_price: float
    key: LegKey


@dataclass(frozen=True)
class _SuggestionLegRef:
    suggestion_id: str
    trade_name: str        # used purely as a notification title hint
    leg_order: int
    action: str
    suggested_price: float
    suggested_price_low: float
    suggested_price_high: float
    key: LegKey


@dataclass
class _Snapshot:
    # All ACTIVE trades grouped by trade_id → list of executed legs.
    trades: Dict[str, List[_TradeLegRef]] = field(default_factory=dict)
    # All today-PENDING suggestions grouped by suggestion_id → list of legs.
    suggestions: Dict[str, List[_SuggestionLegRef]] = field(default_factory=dict)
    # Reverse index: leg_key → list of trade leg refs that match.
    trade_index: Dict[LegKey, List[_TradeLegRef]] = field(default_factory=dict)
    # Reverse index: leg_key → list of suggestion leg refs that match.
    suggestion_index: Dict[LegKey, List[_SuggestionLegRef]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Default DB loader — production callers use this.
# ---------------------------------------------------------------------------
SnapshotLoader = Callable[[], _Snapshot]


def make_db_snapshot_loader(db) -> SnapshotLoader:
    """Build a snapshot loader that reads the live DB.

    The returned callable can be passed to `IntradayMonitor(snapshot_loader=...)`.
    Each call materialises a fresh `_Snapshot` from `options_trades` (ACTIVE)
    and `options_suggestions` (PENDING with `entry_date <= today`).
    """
    from database.models import TradeRepo  # local import — DB is optional in tests

    def _load() -> _Snapshot:
        snap = _Snapshot()
        today = now_ist().date()

        # ---- ACTIVE trades ----
        trd = TradeRepo(db)
        for t in trd.open_trades():
            tid = t["trade_id"]
            trade_name = t.get("trade_name") or tid
            legs_rows = trd.legs_with_suggestion_info(tid)
            legs: List[_TradeLegRef] = []
            for lr in legs_rows:
                if not lr.get("executed"):
                    continue
                fill = lr.get("fill_price")
                if fill is None or float(fill) <= 0:
                    continue
                key = _to_leg_key(
                    symbol=lr["symbol"],
                    expiry=lr["expiry_date"],
                    strike=lr["strike"],
                    option_type=lr["option_type"],
                )
                legs.append(_TradeLegRef(
                    trade_id=tid,
                    trade_name=trade_name,
                    strategy=lr.get("strategy") or "",
                    leg_order=int(lr["leg_order"]),
                    action=str(lr["action"]).upper(),
                    fill_price=float(fill),
                    key=key,
                ))
            if legs:
                snap.trades[tid] = legs
                for ref in legs:
                    snap.trade_index.setdefault(ref.key, []).append(ref)

        # ---- PENDING suggestions for today ----
        # We deliberately do not import SuggestionRepo to avoid coupling;
        # plain SQL is fine here.
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
    """Normalise leg coordinates to the shape used by `LiveQuote`."""
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    return (
        str(symbol),
        expiry,
        float(strike) if strike is not None else None,
        str(option_type).upper() if option_type else None,
    )


# ---------------------------------------------------------------------------
# The monitor
# ---------------------------------------------------------------------------
class IntradayMonitor:
    """Subscribes to TOPIC_TICK, dispatches three alert types via Notifier.

    Parameters
    ----------
    notifier:
        A `notifications.Notifier`. Must expose `notify(notif_type, severity,
        title, body, related_suggestion_id=, related_trade_id=)`.
    snapshot_loader:
        Zero-arg callable returning a fresh `_Snapshot`. Production callers
        pass `make_db_snapshot_loader(db)`. Tests pass an in-memory builder.
    event_bus:
        Defaults to the process singleton.
    sl_multiplier:
        Defaults to STRATEGY_CONFIG["intraday_sl_multiplier"].
    reload_interval_seconds:
        How often to refresh the snapshot. 60 s by default — same cadence as
        the WS subscription manager.
    """

    def __init__(
        self,
        notifier,
        snapshot_loader: SnapshotLoader,
        *,
        event_bus: Optional[EventBus] = None,
        sl_multiplier: Optional[float] = None,
        reload_interval_seconds: float = 60.0,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._notifier = notifier
        self._loader = snapshot_loader
        self._bus = event_bus or get_event_bus()
        self._sl_mult = float(
            sl_multiplier
            if sl_multiplier is not None
            else STRATEGY_CONFIG.get("intraday_sl_multiplier", 2.0)
        )
        self._reload_interval = float(reload_interval_seconds)
        self._clock = clock

        self._snap: _Snapshot = _Snapshot()
        self._last_reload_at: Optional[datetime] = None

        # Latest LTP per leg key.
        self._latest: Dict[LegKey, float] = {}

        # Daily dedup sets — cleared when IST date rolls over.
        self._dedup_date: date = self._clock().date()
        self._sl_alerted: Set[Tuple[str, int]] = set()      # (trade_id, leg_order)
        self._closure_alerted: Set[str] = set()              # trade_id
        self._entry_alerted: Set[str] = set()                # suggestion_id

        self._lock = threading.RLock()
        self._unsub: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Subscribe to the bus and prime the snapshot."""
        with self._lock:
            if self._unsub is not None:
                return
            self._reload_locked()
            self._unsub = self._bus.subscribe(TOPIC_TICK, self.on_tick)
        logger.info("IntradayMonitor: started")

    def stop(self) -> None:
        with self._lock:
            if self._unsub is None:
                return
            try:
                self._unsub()
            finally:
                self._unsub = None
        logger.info("IntradayMonitor: stopped")

    # ------------------------------------------------------------------ tick
    def on_tick(self, quote: LiveQuote) -> None:
        """Public for tests; the bus calls this for every TOPIC_TICK payload."""
        try:
            if quote is None or quote.option_type is None:
                # Spot/index/VIX ticks aren't actionable here. They feed the
                # opportunity-regen path (Phase 2b future), which lives
                # elsewhere.
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

                self._evaluate_active_trades_locked(key, ltp)
                self._evaluate_pending_suggestions_locked(key)
        except Exception:
            # A bug here must NEVER kill the WS thread that calls us.
            logger.exception("IntradayMonitor.on_tick failed for %r", quote)

    # ------------------------------------------------------------------ reload
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
            self._sl_alerted.clear()
            self._closure_alerted.clear()
            self._entry_alerted.clear()

    # ------------------------------------------------------------------ trade-side
    def _evaluate_active_trades_locked(self, key: LegKey, ltp: float) -> None:
        # 1) SL_TRIGGER per short leg matching this tick.
        for ref in self._snap.trade_index.get(key, ()):
            if ref.action != "SELL":
                continue
            threshold = ref.fill_price * self._sl_mult
            if ltp >= threshold and (ref.trade_id, ref.leg_order) not in self._sl_alerted:
                self._sl_alerted.add((ref.trade_id, ref.leg_order))
                self._fire_sl_trigger(ref, ltp, threshold)

        # 2) PERFECT_CLOSURE — for every trade affected by this leg, check
        # whether all SHORT legs are now at-or-below the capture target.
        affected_trades = {
            r.trade_id for r in self._snap.trade_index.get(key, ())
        }
        for tid in affected_trades:
            if tid in self._closure_alerted:
                continue
            if self._is_perfect_closure(tid):
                self._closure_alerted.add(tid)
                self._fire_perfect_closure(tid)

    def _is_perfect_closure(self, trade_id: str) -> bool:
        legs = self._snap.trades.get(trade_id) or []
        short_legs = [l for l in legs if l.action == "SELL"]
        if not short_legs:
            return False  # nothing to capture
        strategy = short_legs[0].strategy
        target_frac = self._take_profit_fraction(strategy)
        for ref in short_legs:
            ltp = self._latest.get(ref.key)
            if ltp is None:
                return False  # haven't seen a tick for every short leg yet
            if ltp > ref.fill_price * (1.0 - target_frac):
                return False
        return True

    @staticmethod
    def _take_profit_fraction(strategy: str) -> float:
        overrides = STRATEGY_CONFIG.get("strategy_take_profit_fraction") or {}
        return float(overrides.get(
            strategy, STRATEGY_CONFIG.get("take_profit_fraction", 0.80)
        ))

    # ------------------------------------------------------------------ suggestion-side
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

    # ------------------------------------------------------------------ dispatch
    def _fire_sl_trigger(self, ref: _TradeLegRef, ltp: float, threshold: float) -> None:
        title = f"{ref.trade_name}: SL trigger on leg {ref.leg_order}"
        body = (
            f"Short leg {ref.leg_order} now ₹{ltp:.2f} "
            f"(entry ₹{ref.fill_price:.2f} × {self._sl_mult:g}× = ₹{threshold:.2f}). "
            "Consider closing this trade."
        )
        try:
            self._notifier.notify(
                "SL_TRIGGER", "CRITICAL", title, body,
                related_trade_id=ref.trade_id,
            )
        except Exception:
            logger.exception("IntradayMonitor: SL_TRIGGER dispatch failed")

    def _fire_perfect_closure(self, trade_id: str) -> None:
        legs = self._snap.trades.get(trade_id) or []
        trade_name = legs[0].trade_name if legs else trade_id
        strategy = legs[0].strategy if legs else ""
        target_frac = self._take_profit_fraction(strategy)
        body = (
            f"All shorts ≤ {int(round((1 - target_frac) * 100))}% of entry "
            f"({int(round(target_frac * 100))}% credit captured). "
            "Close trade now to lock in profit."
        )
        try:
            self._notifier.notify(
                "PERFECT_CLOSURE", "INFO",
                f"{trade_name}: profit target hit", body,
                related_trade_id=trade_id,
            )
        except Exception:
            logger.exception("IntradayMonitor: PERFECT_CLOSURE dispatch failed")

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
