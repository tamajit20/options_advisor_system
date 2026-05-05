"""
lifecycle/opportunity_regen_watcher.py
======================================

Phase 2 — opportunity-regeneration hint on tick.

Runs alongside `IntradayMonitor` inside the WebSocket runner process. Watches
for material intraday changes that mean *today's morning suggestions* may no
longer be the best trade — without actually re-running the suggestion engine
(that needs settled EOD data + IV computation, which only land at 18:30+).

Each tick is checked against the day's first observation for that symbol.
A single `OPPORTUNITY_REGEN_HINT` notification fires per (trigger, symbol)
per IST day when:

* VIX moves more than `STRATEGY_CONFIG["regen_vix_pct_threshold"]` (default
  5%) vs the day's first observed VIX tick.
* Any subscribed underlying spot moves more than
  `STRATEGY_CONFIG["regen_spot_pct_threshold"]` (default 0.7%) vs the day's
  first observed spot tick.

Why baseline = first observed tick (not yesterday's close)?
    The baseline is the price the morning suggestions were built on. When
    the runner starts at 09:15 the first NIFTY tick is, in effect, the
    market's open — i.e. the price the user would have entered at if they
    acted on a pre-market suggestion. Comparing intraday moves against
    that baseline correctly captures "things have moved enough to revisit
    the recommendation" regardless of overnight gap.

What this does NOT do
---------------------
* Does NOT call `run_suggestion_engine` from the tick path. The engine
  needs settled EOD + IV data and is too heavy to run inline. The hint
  notification simply tells the user it might be time to re-run.
* Does NOT track PCR. Live ticks carry LTP/depth but not full chain OI;
  computing live PCR would require rate-limited REST `quote()` calls.
  Deferred to a future enhancement (see FUTURE_ENHANCEMENT_SCOPES.md).

Locked architecture rules
-------------------------
* Read-only Zerodha — no orders, no portfolio reads.
* Fail-open — any exception in `on_tick` is logged and swallowed; the
  publisher thread must never die because of us.
* Notifier persistence is unconditional; gating by `opportunity_alerts`
  flag is handled by `Notifier` itself via `_TYPE_TO_FLAG`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, Optional, Set, Tuple

from config import STRATEGY_CONFIG
from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK, get_event_bus
from utils import now_ist


logger = logging.getLogger(__name__)


# Symbol that the WS subscription manager publishes for INDIA VIX. Must
# match `DEFAULT_INDEX_SPECS[3].internal_symbol` in subscription_manager.py.
_VIX_SYMBOL = "VIX"


# Trigger keys (used for dedup and rendering).
_TRIGGER_VIX = "VIX_MOVE"
_TRIGGER_SPOT = "SPOT_MOVE"
_TRIGGER_IV = "IV_MOVE"


@dataclass
class _Baselines:
    """First observed tick per (symbol, ist_date). Cleared on day rollover."""
    spot:    Dict[Tuple[str, date], float] = field(default_factory=dict)
    iv:      Dict[Tuple[str, date], float] = field(default_factory=dict)
    fired:   Set[Tuple[str, str, date]]    = field(default_factory=set)
    # ^ {(trigger_kind, symbol, ist_date)} — one alert per kind+symbol+day
    last_day: Optional[date] = None


class OpportunityRegenWatcher:
    """Tick-driven regime-shift detector. See module docstring.

    Parameters
    ----------
    notifier:
        Anything exposing `notify(notif_type, severity, title, body, **kw)` —
        in production, `notifications.Notifier`. Tests pass a stub.
    event_bus:
        Optional `EventBus`. Defaults to the process-wide singleton.
    vix_threshold_pct:
        Override for `STRATEGY_CONFIG["regen_vix_pct_threshold"]`.
    spot_threshold_pct:
        Override for `STRATEGY_CONFIG["regen_spot_pct_threshold"]`.
    clock:
        Optional callable returning the current IST datetime. Tests pass a
        controllable clock.
    """

    def __init__(
        self,
        notifier,
        *,
        event_bus: Optional[EventBus] = None,
        vix_threshold_pct: Optional[float] = None,
        spot_threshold_pct: Optional[float] = None,
        iv_threshold_vol_points: Optional[float] = None,
        clock: Callable[[], datetime] = now_ist,
    ):
        self._notifier = notifier
        self._bus = event_bus
        self._clock = clock
        self._vix_threshold = float(
            vix_threshold_pct
            if vix_threshold_pct is not None
            else STRATEGY_CONFIG.get("regen_vix_pct_threshold", 5.0)
        )
        self._spot_threshold = float(
            spot_threshold_pct
            if spot_threshold_pct is not None
            else STRATEGY_CONFIG.get("regen_spot_pct_threshold", 0.7)
        )
        self._iv_threshold = float(
            iv_threshold_vol_points
            if iv_threshold_vol_points is not None
            else STRATEGY_CONFIG.get("regen_iv_pct_threshold", 5.0)
        )
        if self._vix_threshold <= 0 or self._spot_threshold <= 0:
            raise ValueError("thresholds must be positive percentages")
        if self._iv_threshold <= 0:
            raise ValueError("iv threshold must be positive")
        self._lock = threading.Lock()
        self._state = _Baselines()
        self._unsub: Optional[Callable[[], None]] = None
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._started:
            return
        bus = self._bus or get_event_bus()
        self._bus = bus
        self._unsub = bus.subscribe(TOPIC_TICK, self.on_tick)
        self._started = True
        logger.info(
            "OpportunityRegenWatcher started (vix=±%.2f%%, spot=±%.2f%%)",
            self._vix_threshold, self._spot_threshold,
        )

    def stop(self) -> None:
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:
                logger.exception("OpportunityRegenWatcher: unsubscribe failed")
            self._unsub = None
        self._started = False

    # ------------------------------------------------------------------ tick path
    def on_tick(self, quote: LiveQuote) -> None:
        """Top-level entry point — must NEVER raise."""
        try:
            self._on_tick_locked(quote)
        except Exception:
            logger.exception("OpportunityRegenWatcher: on_tick swallowed exception")

    # ------------------------------------------------------------------ IV path
    def on_iv_observation(self, symbol: str, iv_pct: float) -> None:
        """Phase 3 — #1: feed a fresh ATM-IV observation (in vol points,
        e.g. 14.5 means 14.5%). When the day's |Δ IV| crosses
        ``regen_iv_pct_threshold`` we fire ``OPPORTUNITY_REGEN_HINT`` once
        per (symbol, day). Driver code (e.g. a 5-minute scheduler poll
        reading ``options_atm_iv_5min``) should call this method on each
        new observation. Must NEVER raise — fail-open like ``on_tick``."""
        try:
            self._on_iv_locked(symbol, float(iv_pct))
        except Exception:
            logger.exception(
                "OpportunityRegenWatcher: on_iv_observation swallowed exception"
            )

    def _on_iv_locked(self, symbol: str, iv_pct: float) -> None:
        if not symbol or iv_pct <= 0:
            return
        today = self._clock().date()
        with self._lock:
            self._roll_day_locked(today)
            key = (symbol, today)
            baseline = self._state.iv.get(key)
            if baseline is None:
                self._state.iv[key] = iv_pct
                logger.debug(
                    "OpportunityRegenWatcher: IV baseline %s = %.2f",
                    symbol, iv_pct,
                )
                return
            delta = iv_pct - baseline
            if abs(delta) < self._iv_threshold:
                return
            dedup_key = (_TRIGGER_IV, symbol, today)
            if dedup_key in self._state.fired:
                return
            self._state.fired.add(dedup_key)

        # Outside the lock — slow notifier never blocks polling.
        title = (
            f"{symbol} ATM IV moved {delta:+.2f} vol pts — review opportunity"
        )
        body = (
            f"{symbol} IV baseline={baseline:.2f}% current={iv_pct:.2f}% "
            f"Δ={delta:+.2f} (threshold ±{self._iv_threshold:.2f} vol pts)\n"
            f"Morning suggestions for {today} were priced at the baseline "
            f"vol; consider re-running the suggestion engine."
        )
        try:
            self._notifier.notify(
                "OPPORTUNITY_REGEN_HINT",
                "INFO",
                title,
                body,
            )
        except Exception:
            logger.exception(
                "OpportunityRegenWatcher: notifier raised on IV/%s", symbol,
            )

    def _on_tick_locked(self, quote: LiveQuote) -> None:
        # Only spot/index ticks; option ticks have option_type set.
        if getattr(quote, "option_type", None) is not None:
            return
        try:
            ltp = float(getattr(quote, "last_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            return
        if ltp <= 0:
            return
        symbol = str(getattr(quote, "symbol", "") or "")
        if not symbol:
            return

        today = self._clock().date()

        with self._lock:
            self._roll_day_locked(today)
            key = (symbol, today)
            baseline = self._state.spot.get(key)
            if baseline is None:
                self._state.spot[key] = ltp
                logger.debug(
                    "OpportunityRegenWatcher: baseline %s = %.2f", symbol, ltp,
                )
                return
            if baseline <= 0:
                return
            pct = (ltp - baseline) / baseline * 100.0
            abs_pct = abs(pct)

            if symbol == _VIX_SYMBOL:
                trigger = _TRIGGER_VIX
                threshold = self._vix_threshold
            else:
                trigger = _TRIGGER_SPOT
                threshold = self._spot_threshold

            dedup_key = (trigger, symbol, today)
            if dedup_key in self._state.fired:
                return
            if abs_pct < threshold:
                return
            # Mark fired BEFORE dispatching so a slow notifier never causes a
            # second fire (the lock keeps this atomic vs concurrent ticks).
            self._state.fired.add(dedup_key)

        # Notification dispatch happens OUTSIDE the lock — it may take time
        # (channel I/O) and we don't want to block tick processing.
        self._dispatch(symbol, trigger, baseline, ltp, pct, threshold, today)

    # ------------------------------------------------------------------ helpers
    def _roll_day_locked(self, today: date) -> None:
        """If the IST date has changed, clear all baselines + dedup so a new
        trading day starts fresh. Called under the watcher lock."""
        if self._state.last_day is None:
            self._state.last_day = today
            return
        if self._state.last_day == today:
            return
        logger.info(
            "OpportunityRegenWatcher: day rollover %s → %s; clearing state",
            self._state.last_day, today,
        )
        self._state.spot.clear()
        self._state.iv.clear()
        self._state.fired.clear()
        self._state.last_day = today

    def _dispatch(
        self,
        symbol: str,
        trigger: str,
        baseline: float,
        ltp: float,
        pct: float,
        threshold: float,
        today: date,
    ) -> None:
        if trigger == _TRIGGER_VIX:
            title = f"VIX moved {pct:+.2f}% — review opportunity"
        else:
            title = f"{symbol} moved {pct:+.2f}% — review opportunity"
        body = (
            f"{symbol} baseline={baseline:.2f} current={ltp:.2f} "
            f"move={pct:+.2f}% (threshold ±{threshold:.2f}%)\n"
            f"Morning suggestions for {today} were built on a different regime; "
            f"consider re-running the suggestion engine."
        )
        try:
            self._notifier.notify(
                "OPPORTUNITY_REGEN_HINT",
                "INFO",
                title,
                body,
            )
        except Exception:
            logger.exception(
                "OpportunityRegenWatcher: notifier raised on %s/%s", trigger, symbol,
            )
