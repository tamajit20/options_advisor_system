"""
lifecycle/live_risk_monitor.py
==============================

Live, trade-level SL / target alerter.

Subscribes to ``providers.event_bus.TOPIC_TICK`` and, on every option-leg tick
that maps to a leg of an ACTIVE trade, recomputes whole-trade MTM via
``engine.exit_engine.evaluate_exit()`` (the same logic the EOD exit engine
uses) and emits a notification when:

* ``PRE_BREACH_WARNING`` — current loss first crosses
  ``pre_breach_fraction × effective_sl_rs``. Soft warning; gives the user lead
  time before a hard loss limit. Fires once per trade per IST day.
* ``PROFIT_FLOOR_SET`` — a trailing profit-lock step arms and raises the
  trade's ``trailing_pnl_floor``.
* ``PROFIT_FLOOR_HIT`` — live MTM drops below the armed profit floor.
* ``LOSS_LIMIT_HIT`` — current PnL crosses the strategy effective loss limit
  (``effective_sl_rs``).
* ``SL_TRIGGER`` — underlying spot crosses ``actual_stop_loss_level`` (when
  set). Premium-based loss uses ``LOSS_LIMIT_HIT`` instead.
* ``TARGET_HIT`` — current PnL >= ``live_target_fraction × max_profit``,
  where ``live_target_fraction`` is interpolated by DTE (tighter at low DTE).

Alerts are dispatched through the existing ``Notifier`` (which inserts into
``options_notifications`` and respects the ``sl_alerts`` /
``closure_alerts`` runtime flags).

Behaviour
---------
* Never closes the trade automatically. The user closes manually.
* Cooldown re-fire while the trade stays in breach (configurable).
* Cooldown is **reset** when the trade exits the breached state, so the
  next entry into breach alerts immediately.
* Per-trade silencing via ``options_trades.alerts_silenced_until``: when
  set in the future, all alerts for that trade are suppressed until the
  timestamp passes.
* Stale-LTP guard: legs that haven't ticked for ``stale_leg_seconds`` are
  treated as "no fresh price" and trade evaluation is skipped.
* Cold-start prime: optional ``prime_loader`` callable can be supplied to
  populate per-leg LTPs on first reload (avoids waiting for the slowest
  leg's first tick).
* Session guard — only emits during 09:15–15:30 IST.
* Counters surfaced via :meth:`stats` and an optional JSON status file.
* Reload triggers: periodic (default 60 s) **and** the
  ``TOPIC_TRADE_OPENED`` / ``TOPIC_TRADE_CLOSED`` events for prompt updates.

Concurrency
-----------
Tick handling, evaluation, and notify dispatch happen on the publisher's
thread. The shared ``_lock`` guards in-memory state mutations only — it is
**released** before the slow ``Notifier.notify(...)`` call so a flaky
SMTP / Telegram channel cannot block delivery of subsequent ticks.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from config import STRATEGY_CONFIG
from engine.exit_engine import evaluate_exit
from engine.sl_threshold import effective_sl_rs
from providers.base import LiveQuote
from providers.event_bus import (
    EventBus,
    TOPIC_TICK,
    TOPIC_TRADE_CLOSED,
    TOPIC_TRADE_MTM,
    TOPIC_TRADE_OPENED,
    get_event_bus,
)
from utils import days_between, now_ist


logger = logging.getLogger(__name__)


LegKey = Tuple[str, Optional[date], Optional[float], Optional[str]]


# ---------------------------------------------------------------------------
# In-memory views
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _LegRef:
    leg_order: int
    action: str            # "SELL" | "BUY"
    strike: float
    option_type: str       # "CE" | "PE"
    fill_price: float
    lots: int
    lot_size: int
    key: LegKey


@dataclass
class _TradeState:
    trade_id: str
    trade_name: str
    strategy: str
    underlying: str
    expiry: date
    entry_net_credit: float
    max_profit: float
    max_loss: float
    sl_level: Optional[float]
    silenced_until: Optional[datetime] = None
    legs: List[_LegRef] = field(default_factory=list)
    leg_ltps: Dict[LegKey, float] = field(default_factory=dict)
    leg_last_tick: Dict[LegKey, datetime] = field(default_factory=dict)
    last_spot: Optional[float] = None
    last_spot_at: Optional[datetime] = None
    last_alert_at: Dict[str, datetime] = field(default_factory=dict)
    in_breach: Dict[str, bool] = field(default_factory=dict)
    pre_breach_alerted_today: bool = False
    # Phase 3 — #4 trailing SL
    trailing_pnl_floor: Optional[float] = None
    trailing_step_idx: int = 0
    # Phase 3 — #3 MTM throttle
    last_mtm_publish_at: Optional[datetime] = None
    last_mtm_snapshot_at: Optional[datetime] = None


@dataclass
class _Snapshot:
    trades: Dict[str, _TradeState] = field(default_factory=dict)
    index: Dict[LegKey, List[str]] = field(default_factory=dict)
    spot_index: Dict[str, List[str]] = field(default_factory=dict)


SnapshotLoader = Callable[[], _Snapshot]
PrimeLoader = Callable[[List[LegKey]], Dict[LegKey, float]]


# ---------------------------------------------------------------------------
# Default DB loader
# ---------------------------------------------------------------------------
def make_db_snapshot_loader(db) -> SnapshotLoader:
    """Build a snapshot loader that reads ACTIVE trades from the live DB."""
    from database.models import TradeRepo

    def _load() -> _Snapshot:
        snap = _Snapshot()
        trd = TradeRepo(db)
        for trade in trd.open_trades():
            if (trade.get("status") or "").upper() != "ACTIVE":
                continue
            trade_id = trade["trade_id"]

            sug = db.fetch_one(
                "SELECT strategy, underlying, expiry_date "
                "FROM options_suggestions WHERE suggestion_id = ?",
                [trade["suggestion_id"]],
            )
            if not sug:
                continue
            sug_legs = db.fetch_all(
                "SELECT * FROM options_suggestion_legs "
                "WHERE suggestion_id = ? ORDER BY leg_order",
                [trade["suggestion_id"]],
            )
            if not sug_legs:
                continue
            trade_legs = trd.legs(trade_id)
            sug_by_id = {l["id"]: l for l in sug_legs}

            legs: List[_LegRef] = []
            for tl in trade_legs:
                if not tl.get("executed"):
                    continue
                sleg = sug_by_id.get(tl["suggestion_leg_id"])
                if sleg is None:
                    continue
                strike = float(sleg["strike"])
                otype = str(sleg["option_type"]).upper()
                key: LegKey = (
                    str(sug["underlying"]), sug["expiry_date"], strike, otype,
                )
                lots_actual = tl.get("lots_actual")
                lots_sug = sleg.get("lots")
                if not lots_actual and not lots_sug:
                    logger.warning(
                        "LiveRiskMonitor: trade %s leg %s has no lots_actual or "
                        "suggestion lots; defaulting to 1 (MTM will be wrong)",
                        trade_id, tl.get("leg_order"),
                    )
                legs.append(_LegRef(
                    leg_order=int(tl["leg_order"]),
                    action=str(sleg["action"]).upper(),
                    strike=strike,
                    option_type=otype,
                    fill_price=float(tl.get("fill_price") or 0.0),
                    lots=int(lots_actual or lots_sug or 1),
                    lot_size=int(sleg.get("lot_size") or 0),
                    key=key,
                ))
            if not legs:
                continue

            state = _TradeState(
                trade_id=trade_id,
                trade_name=str(trade.get("trade_name") or trade_id),
                strategy=str(sug.get("strategy") or ""),
                underlying=str(sug["underlying"]),
                expiry=sug["expiry_date"],
                entry_net_credit=float(trade.get("net_credit_actual") or 0.0),
                max_profit=float(trade.get("actual_max_profit") or 0.0),
                max_loss=float(trade.get("actual_max_loss") or 0.0),
                sl_level=(float(trade["actual_stop_loss_level"])
                          if trade.get("actual_stop_loss_level") is not None
                          else None),
                silenced_until=trade.get("alerts_silenced_until"),
                legs=legs,
                trailing_pnl_floor=(float(trade["trailing_pnl_floor"])
                                    if trade.get("trailing_pnl_floor") is not None
                                    else None),
                trailing_step_idx=int(trade.get("trailing_step_idx") or 0),
            )
            snap.trades[trade_id] = state
            for leg in legs:
                snap.index.setdefault(leg.key, []).append(trade_id)
            snap.spot_index.setdefault(state.underlying, []).append(trade_id)
        return snap

    return _load


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "enabled":                    True,
    "live_target_fraction":       0.70,
    "cooldown_minutes":           15,
    "reload_interval_sec":        60,
    "session_start":              "09:15",
    "session_end":                "15:30",
    "stale_leg_seconds":          30,
    "pre_breach_fraction":        0.30,
    "target_fraction_at_min_dte": 0.50,
    "target_fraction_at_max_dte": 0.80,
    "target_min_dte":             3,
    "target_max_dte":             15,
    "spot_sl_enabled":            True,
    "dashboard_url":              None,
    "status_path":                None,
    "status_write_interval_sec":  30,
    # Path for the cross-container per-trade MTM state file (polled by Flask SSE).
    # Both containers must mount the same directory (they share data/).
    "mtm_state_path":             "data/live_mtm_state.json",
    # Phase 3 — #4 trailing SL on profit. List of [profit_trigger_fraction,
    # lock_floor_fraction_of_max_profit] tuples, ascending by trigger.
    "trailing_sl_steps":          [[0.50, 0.0], [0.80, 0.40]],
    # Phase 3 — #3 publish-rate throttle (seconds per trade).
    "mtm_publish_interval_sec":   1.0,
    "mtm_snapshot_interval_sec":  900,
    # Phase 3 — #5 tighter pre-breach when there is a HIGH-impact event
    # tomorrow (events_repo provides has_high_impact()).
    "event_eve_pre_breach_fraction": 0.20,
}


def _safe_cfg(raw: Optional[dict]) -> dict:
    """Merge user config with defaults, falling back on bad fields."""
    out = dict(_DEFAULTS)
    if not raw:
        return out
    for key, default in _DEFAULTS.items():
        if key not in raw:
            continue
        val = raw[key]
        try:
            if isinstance(default, bool):
                out[key] = bool(val)
            elif isinstance(default, int) and not isinstance(default, bool):
                out[key] = int(val)
            elif isinstance(default, float):
                out[key] = float(val)
            else:
                out[key] = val
        except (TypeError, ValueError):
            logger.warning(
                "LiveRiskMonitor: invalid config[%s]=%r — using default %r",
                key, val, default,
            )
    for key in ("session_start", "session_end"):
        try:
            _parse_hhmm(out[key])
        except Exception:
            logger.warning(
                "LiveRiskMonitor: invalid config[%s]=%r — using default",
                key, out[key],
            )
            out[key] = _DEFAULTS[key]
    # Sanity check fractions in [0, 1]
    for key in ("pre_breach_fraction", "target_fraction_at_min_dte",
                "target_fraction_at_max_dte", "event_eve_pre_breach_fraction"):
        if not 0.0 <= out[key] <= 1.0:
            logger.warning(
                "LiveRiskMonitor: config[%s]=%r out of [0,1] — using default",
                key, out[key],
            )
            out[key] = _DEFAULTS[key]
    # Trailing SL steps — expect list[[trigger, lock]] with both in [0,1]
    # and triggers strictly ascending. On any malformed entry fall back
    # to defaults rather than raising; trailing is optional.
    raw_steps = out.get("trailing_sl_steps") or []
    safe_steps: list = []
    last_trig = -1.0
    try:
        for step in raw_steps:
            trig, lock = float(step[0]), float(step[1])
            if not (0.0 <= trig <= 1.0 and 0.0 <= lock <= 1.0):
                raise ValueError("out of [0,1]")
            if trig <= last_trig:
                raise ValueError("not ascending")
            safe_steps.append((trig, lock))
            last_trig = trig
    except (TypeError, ValueError, IndexError) as exc:
        logger.warning(
            "LiveRiskMonitor: invalid trailing_sl_steps=%r (%s) — using default",
            raw_steps, exc,
        )
        safe_steps = [tuple(s) for s in _DEFAULTS["trailing_sl_steps"]]
    out["trailing_sl_steps"] = safe_steps
    try:
        out["mtm_publish_interval_sec"] = max(0.0, float(out["mtm_publish_interval_sec"]))
    except (TypeError, ValueError):
        out["mtm_publish_interval_sec"] = float(_DEFAULTS["mtm_publish_interval_sec"])
    try:
        out["mtm_snapshot_interval_sec"] = max(60.0, float(out["mtm_snapshot_interval_sec"]))
    except (TypeError, ValueError):
        out["mtm_snapshot_interval_sec"] = float(_DEFAULTS["mtm_snapshot_interval_sec"])
    return out


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------
class LiveRiskMonitor:
    """Trade-level live SL / target alerter. See module docstring for behaviour."""

    def __init__(
        self,
        *,
        notifier,
        snapshot_loader: SnapshotLoader,
        prime_loader: Optional[PrimeLoader] = None,
        event_bus: Optional[EventBus] = None,
        config: Optional[dict] = None,
        clock: Callable[[], datetime] = now_ist,
        trailing_persister: Optional[Callable[[str, Optional[float], int], None]] = None,
        mtm_snapshot_persister: Optional[Callable[[dict], None]] = None,
        level_event_persister: Optional[Callable[[dict], None]] = None,
        events_repo: Optional[object] = None,
    ) -> None:
        self._notifier = notifier
        self._loader = snapshot_loader
        self._prime = prime_loader
        self._bus = event_bus or get_event_bus()
        cfg = _safe_cfg(config if config is not None
                        else STRATEGY_CONFIG.get("live_risk_monitor"))
        self._enabled = cfg["enabled"]
        self._cooldown = timedelta(minutes=cfg["cooldown_minutes"])
        self._reload_interval = cfg["reload_interval_sec"]
        self._session_start = _parse_hhmm(cfg["session_start"])
        self._session_end = _parse_hhmm(cfg["session_end"])
        self._stale_window = timedelta(seconds=cfg["stale_leg_seconds"])
        self._pre_breach_fraction = cfg["pre_breach_fraction"]
        self._event_eve_pre_breach = cfg["event_eve_pre_breach_fraction"]
        self._target_min_dte = cfg["target_min_dte"]
        self._target_max_dte = cfg["target_max_dte"]
        self._target_min = cfg["target_fraction_at_min_dte"]
        self._target_max = cfg["target_fraction_at_max_dte"]
        self._spot_sl_enabled = cfg["spot_sl_enabled"]
        self._dashboard_url = cfg["dashboard_url"]
        self._status_path    = cfg["status_path"]
        self._mtm_state_path = cfg.get("mtm_state_path") or "data/live_mtm_state.json"
        self._mtm_state: dict = {}   # trade_id → last payload written
        self._status_interval = cfg["status_write_interval_sec"]
        self._trailing_steps: List[Tuple[float, float]] = list(cfg["trailing_sl_steps"])
        self._mtm_publish_interval = timedelta(
            seconds=float(cfg["mtm_publish_interval_sec"] or 0))
        self._mtm_snapshot_interval = timedelta(
            seconds=float(cfg["mtm_snapshot_interval_sec"] or 900))
        self._trailing_persister = trailing_persister
        self._mtm_snapshot_persister = mtm_snapshot_persister
        self._level_event_persister = level_event_persister
        self._events_repo = events_repo
        self._clock = clock

        self._snapshot = _Snapshot()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reload_thread: Optional[threading.Thread] = None
        self._unsubscribers: List[Callable[[], None]] = []
        self._counters: Dict[str, int] = {
            "ticks_in":          0,
            "evaluations":       0,
            "alerts_fired":      0,
            "alerts_suppressed": 0,
            "stale_skips":       0,
            "session_skips":     0,
            "silenced_skips":    0,
            "reloads":           0,
            "trailing_steps_armed": 0,
            "mtm_published":      0,
            "level_events_logged": 0,
        }
        self._last_status_write: Optional[datetime] = None
        self._current_day: Optional[date] = None
        # Event-eve cache: (date_checked, has_event_tomorrow). Refreshed
        # at most once per IST day to avoid hammering the events repo.
        self._event_eve_cache: Tuple[Optional[date], bool] = (None, False)

    # ----- lifecycle -----
    def start(self) -> None:
        if not self._enabled:
            logger.info("LiveRiskMonitor disabled via config")
            return
        self._reload(prime=True)
        self._unsubscribers.append(self._bus.subscribe(TOPIC_TICK, self._on_tick))
        self._unsubscribers.append(
            self._bus.subscribe(TOPIC_TRADE_OPENED, self._on_trade_event))
        self._unsubscribers.append(
            self._bus.subscribe(TOPIC_TRADE_CLOSED, self._on_trade_event))
        self._reload_thread = threading.Thread(
            target=self._reload_loop, name="LiveRiskMonitor.reload", daemon=True)
        self._reload_thread.start()
        logger.info(
            "LiveRiskMonitor started — %d ACTIVE trades watched, "
            "target=%.0f-%.0f%% (DTE %d-%d), cooldown=%dm, stale=%ds",
            len(self._snapshot.trades),
            self._target_min * 100, self._target_max * 100,
            self._target_min_dte, self._target_max_dte,
            int(self._cooldown.total_seconds() / 60),
            int(self._stale_window.total_seconds()),
        )

    def stop(self) -> None:
        self._stop.set()
        for unsub in self._unsubscribers:
            try:
                unsub()
            except Exception:
                logger.exception("LiveRiskMonitor: unsubscribe failed")
        self._unsubscribers.clear()
        if self._reload_thread is not None:
            self._reload_thread.join(timeout=2.0)
            self._reload_thread = None
        logger.info("LiveRiskMonitor stopped")

    def stats(self) -> Dict[str, int]:
        with self._lock:
            out = dict(self._counters)
            out["trades_watched"] = len(self._snapshot.trades)
            return out

    def request_reload(self) -> None:
        """Force an immediate snapshot reload (used after manual ops)."""
        try:
            self._reload()
        except Exception:
            logger.exception("LiveRiskMonitor: manual reload failed")

    # ----- internals -----
    def _reload_loop(self) -> None:
        while not self._stop.wait(self._reload_interval):
            try:
                self._reload()
            except Exception:
                logger.exception("LiveRiskMonitor: reload failed (continuing)")
            self._maybe_write_status()

    def _on_trade_event(self, _payload) -> None:
        try:
            self._reload()
        except Exception:
            logger.exception("LiveRiskMonitor: event-driven reload failed")

    def _reload(self, *, prime: bool = False) -> None:
        new_snap = self._loader()
        with self._lock:
            self._counters["reloads"] += 1
            for tid, new_state in new_snap.trades.items():
                old = self._snapshot.trades.get(tid)
                if old is not None:
                    new_state.last_alert_at = dict(old.last_alert_at)
                    new_state.in_breach = dict(old.in_breach)
                    new_state.pre_breach_alerted_today = old.pre_breach_alerted_today
                    new_keys = {l.key for l in new_state.legs}
                    new_state.leg_ltps = {
                        k: v for k, v in old.leg_ltps.items() if k in new_keys}
                    new_state.leg_last_tick = {
                        k: v for k, v in old.leg_last_tick.items() if k in new_keys}
                    new_state.last_spot = old.last_spot
                    new_state.last_spot_at = old.last_spot_at
                    new_state.last_mtm_publish_at = old.last_mtm_publish_at
                    # Trailing SL: prefer the in-memory state if it's
                    # ahead of what the loader returned (the DB row may
                    # be slightly stale vs ticks since the last UPDATE).
                    if old.trailing_step_idx > new_state.trailing_step_idx:
                        new_state.trailing_step_idx = old.trailing_step_idx
                        new_state.trailing_pnl_floor = old.trailing_pnl_floor
            self._snapshot = new_snap
        if prime and self._prime is not None:
            self._prime_ltps()

    def _prime_ltps(self) -> None:
        with self._lock:
            keys = list(self._snapshot.index.keys())
        if not keys:
            return
        try:
            prices = self._prime(keys) or {}
        except Exception:
            logger.exception(
                "LiveRiskMonitor: prime_loader raised — continuing without prime")
            return
        now = self._clock()
        primed = 0
        with self._lock:
            for key, ltp in prices.items():
                if not isinstance(ltp, (int, float)) or ltp <= 0:
                    continue
                primed += 1
                for tid in self._snapshot.index.get(key, ()):
                    state = self._snapshot.trades.get(tid)
                    if state is not None:
                        state.leg_ltps[key] = float(ltp)
                        state.leg_last_tick[key] = now
        logger.info("LiveRiskMonitor: primed %d/%d legs", primed, len(keys))

    def _on_tick(self, quote: LiveQuote) -> None:
        with self._lock:
            self._counters["ticks_in"] += 1
            self._maybe_reset_for_new_day()

        if quote.strike is None or quote.option_type is None:
            self._handle_spot_tick(quote)
            return
        if quote.expiry is None:
            return

        key: LegKey = (
            quote.symbol, quote.expiry,
            float(quote.strike), str(quote.option_type).upper(),
        )
        ltp = float(quote.last_price or 0.0)
        if ltp <= 0:
            return
        now = self._clock()

        decisions: List[_PendingAlert] = []
        pending_mtm: List[dict] = []
        pending_trail: List[Tuple[str, Optional[float], int]] = []
        pending_snapshots: List[dict] = []
        with self._lock:
            for tid in self._snapshot.index.get(key, ()):
                state = self._snapshot.trades.get(tid)
                if state is None:
                    continue
                state.leg_ltps[key] = ltp
                state.leg_last_tick[key] = now
                alert, mtm, trail, snap = self._evaluate_locked(state, now)
                if alert is not None:
                    decisions.append(alert)
                if mtm is not None:
                    pending_mtm.append(mtm)
                if trail is not None:
                    pending_trail.append(trail)
                if snap is not None:
                    pending_snapshots.append(snap)

        for d in decisions:
            self._dispatch(d)
        for payload in pending_mtm:
            try:
                self._bus.publish(TOPIC_TRADE_MTM, payload)
                self._write_mtm_state(payload)
            except Exception:
                logger.exception("LiveRiskMonitor: TOPIC_TRADE_MTM publish failed")
        for snap in pending_snapshots:
            if self._mtm_snapshot_persister is not None:
                try:
                    self._mtm_snapshot_persister(snap)
                except Exception:
                    logger.exception(
                        "LiveRiskMonitor: MTM snapshot persist failed for %s",
                        snap.get("trade_id"),
                    )
        for args in pending_trail:
            self._persist_trailing(*args)

    def _handle_spot_tick(self, quote: LiveQuote) -> None:
        if not self._spot_sl_enabled:
            return
        ltp = float(quote.last_price or 0.0)
        if ltp <= 0:
            return
        now = self._clock()
        decisions: List[_PendingAlert] = []
        with self._lock:
            for tid in self._snapshot.spot_index.get(quote.symbol, ()):
                state = self._snapshot.trades.get(tid)
                if state is None:
                    continue
                state.last_spot = ltp
                state.last_spot_at = now
                if state.sl_level is None or state.sl_level <= 0:
                    continue
                if not self._in_session(now):
                    continue
                if state.silenced_until is not None and now < state.silenced_until:
                    self._counters["silenced_skips"] += 1
                    continue
                breached = self._spot_breached(state, ltp)
                key = "SPOT_SL"
                was = state.in_breach.get(key, False)
                if breached:
                    last = state.last_alert_at.get(key)
                    if (not was) or last is None or (now - last) >= self._cooldown:
                        if not was:
                            self._record_level_transition(
                                state, key, "ENTER", now,
                                self._current_pnl(state),
                                threshold_rs=state.sl_level,
                                spot=ltp, include_legs=True,
                            )
                        state.in_breach[key] = True
                        state.last_alert_at[key] = now
                        decisions.append(_PendingAlert(
                            state=state, notif_type="SL_TRIGGER",
                            severity="CRITICAL",
                            title=f"Spot SL hit on {state.trade_name}",
                            body=self._format_spot_body(state, ltp),
                            breach_key=key,
                        ))
                else:
                    if was:
                        self._record_level_transition(
                            state, key, "EXIT", now,
                            self._current_pnl(state),
                            threshold_rs=state.sl_level,
                            spot=ltp,
                        )
                        # Reset cooldown so a new breach alerts immediately.
                        state.in_breach[key] = False
                        state.last_alert_at.pop(key, None)
        for d in decisions:
            self._dispatch(d)

    @staticmethod
    def _spot_breached(state: _TradeState, spot: float) -> bool:
        from contracts import SuggestionLeg
        from engine.stop_loss_levels import spot_stop_breached

        sug_legs = [
            SuggestionLeg(
                leg_order=i + 1, hedge_pair_leg=None, symbol=state.underlying,
                expiry_date=state.expiry, strike=l.strike, option_type=l.option_type,
                action=l.action, lots=l.lots, lot_size=l.lot_size,
                suggested_price=l.fill_price, suggested_price_low=l.fill_price,
                suggested_price_high=l.fill_price, leg_purpose_note="",
            )
            for i, l in enumerate(state.legs)
        ]
        qty = sum(l.lots * l.lot_size for l in state.legs) or 1
        np_ps = state.entry_net_credit / qty if qty else 0.0
        return spot_stop_breached(
            strategy=state.strategy or "",
            spot=spot,
            legs=sug_legs,
            stored_sl_level=state.sl_level,
            net_premium_per_share=np_ps,
        )

    def _feed_source(self, state: _TradeState, now: datetime) -> str:
        for leg in state.legs:
            last = state.leg_last_tick.get(leg.key)
            if last is None or (now - last) > self._stale_window:
                return "stale"
        return "live"

    def _evaluate_locked(
        self, state: _TradeState, now: datetime,
    ) -> Tuple[
        Optional["_PendingAlert"], Optional[dict],
        Optional[Tuple[str, Optional[float], int]], Optional[dict],
    ]:
        """Returns (alert, mtm_payload, trailing_persist_args, snapshot_payload).

        ``mtm_payload`` is non-None when this evaluation should publish on
        TOPIC_TRADE_MTM after the lock is released. ``trailing_persist_args``
        is non-None when a trailing-step ratchet must be persisted to DB.
        ``snapshot_payload`` is non-None when a periodic DB snapshot should
        be written.
        """
        # Stale guard.
        for leg in state.legs:
            last = state.leg_last_tick.get(leg.key)
            if last is None:
                return None, None, None, None
            if (now - last) > self._stale_window:
                self._counters["stale_skips"] += 1
                return None, None, None, None
        if not self._in_session(now):
            self._counters["session_skips"] += 1
            return None, None, None, None
        if state.silenced_until is not None and now < state.silenced_until:
            self._counters["silenced_skips"] += 1
            return None, None, None, None

        self._counters["evaluations"] += 1

        current_chain = [
            {"strike": leg.strike, "option_type": leg.option_type,
             "mid_price": state.leg_ltps[leg.key]}
            for leg in state.legs
        ]
        legs_for_engine = [
            {"action": leg.action, "strike": leg.strike,
             "option_type": leg.option_type, "fill_price": leg.fill_price,
             "lots": leg.lots, "lot_size": leg.lot_size}
            for leg in state.legs
        ]
        dte = max(days_between(now.date(), state.expiry), 0)

        decision = evaluate_exit(
            trade_id=state.trade_id,
            legs=legs_for_engine,
            current_chain=current_chain,
            entry_net_credit=state.entry_net_credit,
            max_profit_rs=state.max_profit,
            max_loss_rs=state.max_loss,
            sl_level_per_share=state.sl_level,
            days_to_expiry=dte,
            strategy=state.strategy,
            as_of=now,
        )

        current_pnl = self._current_pnl(state)
        target_fraction = self._dte_target_fraction(dte)
        active_pre_breach = self._active_pre_breach_fraction(now)
        sl_threshold, sl_label = effective_sl_rs(
            strategy=state.strategy, max_loss_rs=state.max_loss)
        target_rs = (
            target_fraction * state.max_profit
            if state.max_profit > 0 else None
        )

        # Trailing SL ratchet (#4): when current_pnl crosses the next step's
        # trigger (= step_trigger × max_profit), bump the floor up.
        trailing_persist: Optional[Tuple[str, Optional[float], int]] = None
        trailing_lock_alert: Optional[_PendingAlert] = None
        if state.max_profit > 0 and self._trailing_steps:
            while state.trailing_step_idx < len(self._trailing_steps):
                trig, lock = self._trailing_steps[state.trailing_step_idx]
                if current_pnl < trig * state.max_profit:
                    break
                # Arm this step.
                new_floor = lock * state.max_profit
                # Only ratchet up — never lower an existing floor.
                if (state.trailing_pnl_floor is None
                        or new_floor > state.trailing_pnl_floor):
                    state.trailing_pnl_floor = new_floor
                state.trailing_step_idx += 1
                self._counters["trailing_steps_armed"] += 1
                trailing_persist = (
                    state.trade_id, state.trailing_pnl_floor,
                    state.trailing_step_idx,
                )
                trailing_lock_alert = _PendingAlert(
                    state=state, notif_type="PROFIT_FLOOR_SET", severity="INFO",
                    title=f"Profit floor set on {state.trade_name}",
                    body=self._format_pnl_body(
                        state, current_pnl,
                        f"Reached {trig*100:.0f}% of max profit; profit floor "
                        f"raised to ₹{state.trailing_pnl_floor:,.0f}",
                    ),
                    breach_key=f"TRAIL_{state.trailing_step_idx}",
                )

        # MTM publish (#3) — throttled per trade.
        mtm_payload: Optional[dict] = None
        if (self._mtm_publish_interval.total_seconds() <= 0
                or state.last_mtm_publish_at is None
                or (now - state.last_mtm_publish_at) >= self._mtm_publish_interval):
            state.last_mtm_publish_at = now
            self._counters["mtm_published"] += 1
            mtm_payload = {
                "trade_id": state.trade_id,
                "trade_name": state.trade_name,
                "mtm": round(current_pnl, 2),
                "dte": dte,
                "max_profit": state.max_profit,
                "max_loss": state.max_loss,
                "trailing_pnl_floor": state.trailing_pnl_floor,
                "as_of": now.isoformat(timespec="seconds"),
                # Per-leg live prices keyed by "symbol|strike|option_type"
                # (LegKey is (symbol, expiry, strike, option_type) so we use
                #  indices 0, 2, 3 to match the frontend legKey format)
                "leg_ltps": {
                    f"{k[0]}|{k[2]}|{k[3]}": round(v, 2)
                    for k, v in state.leg_ltps.items()
                },
            }

        snapshot_payload: Optional[dict] = None
        if (self._mtm_snapshot_persister is not None
                and (state.last_mtm_snapshot_at is None
                     or (now - state.last_mtm_snapshot_at) >= self._mtm_snapshot_interval)):
            state.last_mtm_snapshot_at = now
            snapshot_payload = {
                "trade_id": state.trade_id,
                "trade_name": state.trade_name,
                "mtm": round(current_pnl, 2),
                "dte": dte,
                "max_profit": state.max_profit,
                "max_loss": state.max_loss,
                "as_of": now.isoformat(timespec="seconds"),
                "leg_ltps": {
                    f"{k[0]}|{k[2]}|{k[3]}": round(v, 2)
                    for k, v in state.leg_ltps.items()
                },
                "feed_source": self._feed_source(state, now),
            }

        trailing_breach = (
            state.trailing_pnl_floor is not None
            and current_pnl < state.trailing_pnl_floor
        )

        # 1a. Hard loss limit (premium-based SL from exit engine).
        if decision.decision == "SL_HIT":
            alert = self._maybe_alert(
                state, "LOSS_LIMIT_HIT", "CRITICAL",
                title=f"Loss limit hit on {state.trade_name}",
                body=self._format_pnl_body(
                    state, current_pnl, decision.reason,
                ),
                breach_key="LOSS_LIMIT", now=now,
                threshold_rs=sl_threshold,
            )
            return alert, mtm_payload, trailing_persist, snapshot_payload

        # 1b. Trailing profit floor breach (separate breach bucket / cooldown).
        if trailing_breach:
            reason = (
                f"Live MTM ₹{current_pnl:,.0f} fell below profit floor "
                f"₹{state.trailing_pnl_floor:,.0f}"
            )
            alert = self._maybe_alert(
                state, "PROFIT_FLOOR_HIT", "WARNING",
                title=f"Profit floor hit on {state.trade_name}",
                body=self._format_pnl_body(state, current_pnl, reason),
                breach_key="PROFIT_FLOOR", now=now,
                threshold_rs=state.trailing_pnl_floor,
            )
            return alert, mtm_payload, trailing_persist, snapshot_payload

        # 2. Soft pre-breach warning. Event-eve uses tighter fraction.
        pre_breach_rs = active_pre_breach * sl_threshold
        if (sl_threshold > 0
                and current_pnl <= -pre_breach_rs
                and not state.pre_breach_alerted_today):
            state.pre_breach_alerted_today = True
            reason = (f"Loss ≥ {active_pre_breach * 100:.0f}% of SL threshold "
                      f"(₹{sl_threshold:,.0f}, {sl_label}): "
                      f"₹{current_pnl:,.0f}")
            state.in_breach["PRE_BREACH"] = True
            state.last_alert_at["PRE_BREACH"] = now
            return _PendingAlert(
                state=state, notif_type="PRE_BREACH_WARNING", severity="WARNING",
                title=f"Approaching SL on {state.trade_name}",
                body=self._format_pnl_body(state, current_pnl, reason),
                breach_key="PRE_BREACH",
            ), mtm_payload, trailing_persist, snapshot_payload

        # 3. Target hit (DTE-aware fraction).
        if (decision.decision == "TAKE_PROFIT"
                and state.max_profit > 0
                and current_pnl >= target_fraction * state.max_profit):
            return self._maybe_alert(
                state, "TARGET_HIT", "INFO",
                title=f"Target reached on {state.trade_name}",
                body=self._format_pnl_body(
                    state, current_pnl,
                    f"{decision.reason} (live target {target_fraction*100:.0f}%)",
                ),
                breach_key="TARGET", now=now,
                threshold_rs=target_rs,
            ), mtm_payload, trailing_persist, snapshot_payload

        # 4. Not in breach — clear so cooldown resets on re-entry.
        if decision.decision != "SL_HIT":
            self._clear_level_breach(
                state, "LOSS_LIMIT", now, current_pnl, sl_threshold)
        if not trailing_breach:
            self._clear_level_breach(
                state, "PROFIT_FLOOR", now, current_pnl,
                state.trailing_pnl_floor)
        self._clear_level_breach(state, "TARGET", now, current_pnl, target_rs)
        # If we armed a trailing step but no other alert fired, surface
        # the lock confirmation. Otherwise return only the MTM payload.
        if trailing_lock_alert is not None:
            return trailing_lock_alert, mtm_payload, trailing_persist, snapshot_payload
        return None, mtm_payload, trailing_persist, snapshot_payload

    def _maybe_alert(
        self, state: _TradeState, notif_type: str, severity: str,
        *, title: str, body: str, breach_key: str, now: datetime,
        threshold_rs: Optional[float] = None,
    ) -> Optional["_PendingAlert"]:
        was = state.in_breach.get(breach_key, False)
        last = state.last_alert_at.get(breach_key)
        in_cooldown = (last is not None and (now - last) < self._cooldown)
        # Suppress if still in cooldown AND we were already in breach
        # (i.e. this isn't a re-entry after recovery).
        if was and in_cooldown:
            self._counters["alerts_suppressed"] += 1
            return None
        if not was:
            self._record_level_transition(
                state, breach_key, "ENTER", now,
                self._current_pnl(state),
                threshold_rs=threshold_rs,
                include_legs=True,
            )
        state.in_breach[breach_key] = True
        state.last_alert_at[breach_key] = now
        return _PendingAlert(
            state=state, notif_type=notif_type, severity=severity,
            title=title, body=body, breach_key=breach_key,
        )

    _LEVEL_EVENT_KEYS = {
        "TARGET": "TARGET",
        "PROFIT_FLOOR": "PROFIT_FLOOR",
        "LOSS_LIMIT": "LOSS_LIMIT",
        "SPOT_SL": "SPOT_SL",
    }

    def _leg_ltps_dict(self, state: _TradeState) -> Dict[str, float]:
        return {
            f"{k[0]}|{k[2]}|{k[3]}": round(v, 2)
            for k, v in state.leg_ltps.items()
        }

    def _record_level_transition(
        self,
        state: _TradeState,
        breach_key: str,
        event_type: str,
        now: datetime,
        mtm: float,
        *,
        threshold_rs: Optional[float] = None,
        spot: Optional[float] = None,
        include_legs: bool = False,
    ) -> None:
        """Persist ENTER/EXIT for post-trade analysis (whip-saw, time-in-breach)."""
        if self._level_event_persister is None:
            return
        level = self._LEVEL_EVENT_KEYS.get(breach_key)
        if not level:
            return
        payload: dict = {
            "trade_id": state.trade_id,
            "level_type": level,
            "event_type": event_type,
            "event_at": now,
            "mtm": round(float(mtm), 2),
            "threshold_rs": threshold_rs,
            "spot": spot if spot is not None else state.last_spot,
        }
        if include_legs and event_type == "ENTER":
            legs = self._leg_ltps_dict(state)
            if legs:
                payload["leg_ltps"] = legs
        try:
            self._level_event_persister(payload)
            with self._lock:
                self._counters["level_events_logged"] += 1
        except Exception:
            logger.exception(
                "LiveRiskMonitor: level_event_persister failed for %s/%s",
                state.trade_id, level,
            )

    def _clear_level_breach(
        self,
        state: _TradeState,
        breach_key: str,
        now: datetime,
        mtm: float,
        threshold_rs: Optional[float],
    ) -> None:
        if not state.in_breach.get(breach_key):
            return
        self._record_level_transition(
            state, breach_key, "EXIT", now, mtm, threshold_rs=threshold_rs,
        )
        state.in_breach[breach_key] = False
        state.last_alert_at.pop(breach_key, None)

    def _dispatch(self, alert: "_PendingAlert") -> None:
        try:
            self._notifier.notify(
                notif_type=alert.notif_type,
                severity=alert.severity,
                title=alert.title,
                body=alert.body,
                related_trade_id=alert.state.trade_id,
            )
            with self._lock:
                self._counters["alerts_fired"] += 1
        except Exception:
            logger.exception(
                "LiveRiskMonitor: notify failed for %s/%s",
                alert.state.trade_id, alert.notif_type,
            )

    def _current_pnl(self, state: _TradeState) -> float:
        # entry_net_credit is signed (positive=credit, negative=debit), so
        # the same formula works for both credit and debit strategies.
        total = state.entry_net_credit
        for leg in state.legs:
            ltp = state.leg_ltps.get(leg.key, 0.0)
            qty = leg.lots * leg.lot_size
            sign = -1.0 if leg.action == "SELL" else 1.0
            total += sign * ltp * qty
        return total

    def _dte_target_fraction(self, dte: int) -> float:
        if dte <= self._target_min_dte:
            return self._target_min
        if dte >= self._target_max_dte:
            return self._target_max
        span = self._target_max_dte - self._target_min_dte
        if span <= 0:
            return self._target_max
        f = (dte - self._target_min_dte) / span
        return self._target_min + f * (self._target_max - self._target_min)

    def _active_pre_breach_fraction(self, now: datetime) -> float:
        """Returns the pre-breach loss fraction to use right now.

        Falls back to the standard ``pre_breach_fraction`` unless an
        ``events_repo`` was supplied AND it reports a HIGH-impact event
        scheduled for tomorrow (today + 1). In that case the tighter
        ``event_eve_pre_breach_fraction`` applies. Cached per IST day to
        keep the hot tick path off the DB."""
        if self._events_repo is None:
            return self._pre_breach_fraction
        today = now.date()
        cached_day, has_event = self._event_eve_cache
        if cached_day != today:
            try:
                tomorrow = today + timedelta(days=1)
                has_event = bool(
                    self._events_repo.has_high_impact(tomorrow, tomorrow))
            except Exception:
                logger.exception("LiveRiskMonitor: events_repo lookup failed")
                has_event = False
            self._event_eve_cache = (today, has_event)
        return self._event_eve_pre_breach if has_event else self._pre_breach_fraction

    def _persist_trailing(
        self, trade_id: str, floor: Optional[float], step_idx: int,
    ) -> None:
        """Best-effort persistence of trailing SL state. Failure is logged
        and swallowed: in-memory state remains correct so alerts continue
        to fire even if the DB write transiently fails."""
        if self._trailing_persister is None:
            return
        try:
            self._trailing_persister(trade_id, floor, step_idx)
        except Exception:
            logger.exception(
                "LiveRiskMonitor: trailing_persister raised for %s", trade_id)

    def _format_pnl_body(self, state: _TradeState, current_pnl: float, reason: str) -> str:
        body = (
            f"{state.strategy} {state.underlying} exp={state.expiry}: "
            f"live MTM ₹{current_pnl:,.0f} (max profit ₹{state.max_profit:,.0f}, "
            f"max loss ₹{state.max_loss:,.0f}). {reason}. "
            f"Close manually if you agree."
        )
        return self._with_link(body, state)

    def _format_spot_body(self, state: _TradeState, spot: float) -> str:
        body = (
            f"{state.strategy} {state.underlying} exp={state.expiry}: "
            f"spot ₹{spot:,.2f} crossed SL level ₹{state.sl_level:,.2f}. "
            f"Close manually if you agree."
        )
        return self._with_link(body, state)

    def _with_link(self, body: str, state: _TradeState) -> str:
        if not self._dashboard_url:
            return body
        return f"{body}  →  {self._dashboard_url.rstrip('/')}/#/trade/{state.trade_id}"

    def _in_session(self, now: datetime) -> bool:
        # All times are interpreted as IST; the `clock` injected at construction
        # must return IST-naive datetimes (utils.now_ist).
        t = now.time()
        return self._session_start <= t <= self._session_end

    def _maybe_reset_for_new_day(self) -> None:
        today = self._clock().date()
        if self._current_day == today:
            return
        self._current_day = today
        for state in self._snapshot.trades.values():
            state.pre_breach_alerted_today = False

    def _write_mtm_state(self, payload: dict) -> None:
        """Write per-trade MTM to a shared file so the Flask dashboard
        container (separate process) can poll it for the live MTM SSE stream."""
        try:
            self._mtm_state[payload["trade_id"]] = payload
            path = self._mtm_state_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "as_of":  payload["as_of"],
                    "trades": self._mtm_state,
                }, f)
            os.replace(tmp, path)
        except Exception:
            pass  # never let file I/O interrupt the monitor loop

    def _maybe_write_status(self) -> None:
        if not self._status_path:
            return
        now = self._clock()
        if (self._last_status_write is not None and
                (now - self._last_status_write).total_seconds() < self._status_interval):
            return
        self._last_status_write = now
        try:
            payload = {
                "as_of": now.isoformat(timespec="seconds"),
                "stats": self.stats(),
            }
            os.makedirs(os.path.dirname(self._status_path) or ".", exist_ok=True)
            tmp = self._status_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self._status_path)
        except Exception:
            logger.exception("LiveRiskMonitor: status write failed (continuing)")


@dataclass
class _PendingAlert:
    state: _TradeState
    notif_type: str
    severity: str
    title: str
    body: str
    breach_key: str


def _parse_hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))
