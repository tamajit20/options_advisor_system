"""
providers/zerodha/subscription_manager.py
=========================================

Phase 2b-ii — Dynamic subscription manager for the WebSocket runner.

Why a separate manager?
    `KiteWSRunner` is intentionally dumb about *what* to subscribe to. It only
    knows how to apply a desired token set. The manager is the policy layer:
    it polls the database (and optionally, pre-loaded indexes) on a fixed
    cadence, computes the union of tokens that any consumer in the system
    cares about right now, and pushes that set into the runner.

What gets subscribed?
    1. Option legs of every ACTIVE row in `options_trades` (via the parent
       suggestion's `options_suggestion_legs`) — needed for SL alerts.
    2. Option legs of every PENDING row in `options_suggestions` for *today*
       — needed for PERFECT_ENTRY re-evaluation.
    3. The index spots configured in `STRATEGY_CONFIG["underlyings"]`
       (NIFTY 50 / NIFTY BANK / NIFTY FIN SERVICE) plus INDIA VIX — needed
       for opportunity regeneration.

If the resulting set is empty the manager pushes an empty set, which causes
the runner to unsubscribe everything. The runner remains connected; the
operator can decide separately whether to stop the WS process.

Design notes
    * Loaders are injected (callables). DB-backed factory helpers are at the
      bottom of this module, but tests can pass any iterable.
    * Reconcile is idempotent: it diffs the new set against the runner's
      `desired_tokens()` and only calls `replace_subscriptions()` when the
      set actually changed. This keeps the WS quiet during steady state.
    * The poll loop runs in its own daemon thread. It does not own the WS
      connection — `KiteWSRunner` does — so a slow DB query never blocks
      the tick stream.
    * Errors during a reconcile are logged and swallowed; the next tick of
      the loop will retry. This is by design: we never want a transient
      DB blip to take down the live data stream.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from utils import now_ist
from typing import Callable, Iterable, Iterator, List, Optional, Set, Tuple

from .instruments import InstrumentMaster
from .ws_runner import KiteWSRunner, TokenMeta


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Loader contracts
# ---------------------------------------------------------------------------

# Each option leg is a 4-tuple: (underlying, expiry, strike, option_type).
#   underlying   — "NIFTY" / "BANKNIFTY" / "FINNIFTY"
#   expiry       — date
#   strike       — float
#   option_type  — "CE" / "PE"
OptionLegKey = Tuple[str, date, float, str]
LegLoader = Callable[[], Iterable[OptionLegKey]]


@dataclass(frozen=True)
class IndexSpec:
    """Static description of an index/spot we want to stream.

    `tradingsymbol` matches the entry in Kite's instrument master under
    exchange `NSE` (or `MCX`/`BSE` if extended later). `internal_symbol`
    is the canonical name we use in the rest of the system (DB tables,
    LiveQuote.symbol, cache keys).
    """
    internal_symbol: str
    exchange: str
    tradingsymbol: str


IndexLoader = Callable[[], Iterable[IndexSpec]]


# Default indexes streamed for opportunity regeneration. Tied to
# `STRATEGY_CONFIG["underlyings"]` plus INDIA VIX.
DEFAULT_INDEX_SPECS: Tuple[IndexSpec, ...] = (
    IndexSpec("NIFTY",     "NSE", "NIFTY 50"),
    IndexSpec("BANKNIFTY", "NSE", "NIFTY BANK"),
    IndexSpec("FINNIFTY",  "NSE", "NIFTY FIN SERVICE"),
    IndexSpec("VIX",       "NSE", "INDIA VIX"),
)


# ---------------------------------------------------------------------------
# Subscription manager
# ---------------------------------------------------------------------------

@dataclass
class SubscriptionStatus:
    """Snapshot of the manager's last reconcile, exposed for diagnostics."""
    last_reconcile_at: Optional[datetime]
    last_token_count: int
    last_unresolved_legs: int
    last_error: Optional[str]
    reconcile_count: int


class SubscriptionManager:
    """Recompute and apply the WS runner's desired token set on a fixed cadence.

    Parameters
    ----------
    runner:
        The `KiteWSRunner` instance to drive. Must expose
        `set_token_meta()`, `replace_subscriptions()`, and `desired_tokens()`.
    instrument_master:
        Already-refreshed `InstrumentMaster`. Used to translate option-leg
        keys and index trading-symbols into Kite `instrument_token`s.
    leg_loader:
        Zero-arg callable returning option-leg keys for everything we
        currently care about (active trades + pending suggestions + …).
    index_loader:
        Zero-arg callable returning `IndexSpec` rows. Defaults to a
        constant supplier of `DEFAULT_INDEX_SPECS`.
    interval_seconds:
        Poll cadence (default 60s).
    """

    def __init__(
        self,
        runner: KiteWSRunner,
        instrument_master: InstrumentMaster,
        leg_loader: LegLoader,
        index_loader: Optional[IndexLoader] = None,
        *,
        interval_seconds: float = 60.0,
        kill_switch_fn: Optional[Callable[[], bool]] = None,
    ):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._runner = runner
        self._master = instrument_master
        self._leg_loader = leg_loader
        self._index_loader: IndexLoader = (
            index_loader if index_loader is not None else (lambda: DEFAULT_INDEX_SPECS)
        )
        self._interval = float(interval_seconds)
        # `kill_switch_fn()` returns True when live data is globally disabled.
        # When True, we apply an empty token set every cycle. The runner stays
        # connected (so flipping the switch back on resumes immediately).
        self._kill_switch_fn: Callable[[], bool] = kill_switch_fn or (lambda: False)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._status = SubscriptionStatus(
            last_reconcile_at=None,
            last_token_count=0,
            last_unresolved_legs=0,
            last_error=None,
            reconcile_count=0,
        )

    # ------------------------------------------------------------------ public
    def status(self) -> SubscriptionStatus:
        """Return a snapshot of the most recent reconcile."""
        with self._lock:
            return self._status

    def reconcile_once(self) -> Set[int]:
        """Synchronous single reconcile pass. Returns the token set applied.

        Safe to call from tests or an admin hook. Errors during loading or
        InstrumentMaster lookups are surfaced through `status().last_error`
        and re-raised, so callers can decide whether to retry. The loop in
        `_run()` swallows them instead.
        """
        try:
            self._master.refresh_if_stale()
        except Exception as exc:  # pragma: no cover - delegated to InstrumentMaster
            self._record_error(f"instrument refresh failed: {exc}")
            raise

        # Kill switch short-circuits the loader work.
        try:
            kill = bool(self._kill_switch_fn())
        except Exception as exc:
            logger.warning("subscription_manager: kill_switch_fn raised: %s", exc)
            kill = False

        if kill:
            tokens: Set[int] = set()
            unresolved = 0
            current = self._runner.desired_tokens()
            if tokens != current:
                self._runner.replace_subscriptions(tokens)
                logger.warning(
                    "subscription_manager: kill_switch=ON, unsubscribed %d tokens",
                    len(current),
                )
            with self._lock:
                self._status = SubscriptionStatus(
                    last_reconcile_at=now_ist(),
                    last_token_count=0,
                    last_unresolved_legs=0,
                    last_error=None,
                    reconcile_count=self._status.reconcile_count + 1,
                )
            return tokens

        legs = list(self._leg_loader())
        indexes = list(self._index_loader())

        tokens = set()
        unresolved = 0

        for spec in indexes:
            inst = self._master.get_by_tradingsymbol(spec.exchange, spec.tradingsymbol)
            if inst is None:
                logger.warning(
                    "subscription_manager: index %s/%s not in instrument master",
                    spec.exchange, spec.tradingsymbol,
                )
                unresolved += 1
                continue
            tokens.add(inst.instrument_token)
            self._runner.set_token_meta(
                inst.instrument_token,
                TokenMeta(symbol=spec.internal_symbol, is_index=True),
            )

        for (underlying, expiry, strike, opt_type) in legs:
            inst = self._master.get_option(underlying, expiry, float(strike), opt_type)
            if inst is None:
                logger.warning(
                    "subscription_manager: option not in master: %s %s %s %s",
                    underlying, expiry, strike, opt_type,
                )
                unresolved += 1
                continue
            tokens.add(inst.instrument_token)
            # Convert the date-only expiry into a datetime so the runner's
            # `LiveQuote` carries the same shape regardless of source.
            expiry_dt = (
                datetime(expiry.year, expiry.month, expiry.day)
                if isinstance(expiry, date)
                else expiry
            )
            self._runner.set_token_meta(
                inst.instrument_token,
                TokenMeta(
                    symbol=underlying,
                    expiry=expiry_dt,
                    strike=float(strike),
                    option_type=opt_type,
                    is_index=False,
                ),
            )

        current = self._runner.desired_tokens()
        if tokens != current:
            self._runner.replace_subscriptions(tokens)
            logger.info(
                "subscription_manager: applied %d tokens (was %d, +%d / -%d)",
                len(tokens), len(current),
                len(tokens - current), len(current - tokens),
            )
        else:
            logger.debug("subscription_manager: no change (%d tokens)", len(tokens))

        with self._lock:
            self._status = SubscriptionStatus(
                last_reconcile_at=now_ist(),
                last_token_count=len(tokens),
                last_unresolved_legs=unresolved,
                last_error=None,
                reconcile_count=self._status.reconcile_count + 1,
            )
        return tokens

    def start(self) -> None:
        """Spawn the background poll thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="ws-subscription-manager",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "subscription_manager: started, interval=%.1fs", self._interval
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit and wait for the thread."""
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
            if t.is_alive():
                logger.warning("subscription_manager: thread did not exit in %.1fs", timeout)
        self._thread = None

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        # Run an immediate reconcile so we don't wait `interval_seconds`
        # before the first subscription is applied.
        try:
            self.reconcile_once()
        except Exception as exc:
            logger.exception("subscription_manager: initial reconcile failed: %s", exc)
            self._record_error(str(exc))

        while not self._stop_event.wait(self._interval):
            try:
                self.reconcile_once()
            except Exception as exc:
                logger.exception("subscription_manager: reconcile failed: %s", exc)
                self._record_error(str(exc))

    def _record_error(self, msg: str) -> None:
        with self._lock:
            self._status = SubscriptionStatus(
                last_reconcile_at=self._status.last_reconcile_at,
                last_token_count=self._status.last_token_count,
                last_unresolved_legs=self._status.last_unresolved_legs,
                last_error=msg,
                reconcile_count=self._status.reconcile_count,
            )


# ---------------------------------------------------------------------------
# DB-backed loader factories
# ---------------------------------------------------------------------------

def make_db_leg_loader(db) -> LegLoader:
    """Return a loader that reads option legs from SQL Server.

    The loader yields the union of:
      * legs of suggestions whose trades are ACTIVE
      * legs of suggestions that are still PENDING and were generated today

    `db` is a `SQLServerConnection` (anything exposing `fetch_all`).

    The query uses today's date in the connection's local timezone. Phase 4
    will replace this with a runtime-flag-aware loader that can return an
    empty set when the operator flips the kill switch.
    """
    sql = """
    -- Active trade legs
    SELECT DISTINCT
        sl.symbol      AS symbol,
        sl.expiry_date AS expiry_date,
        sl.strike      AS strike,
        sl.option_type AS option_type
    FROM options_trades       t
    JOIN options_suggestion_legs sl ON sl.suggestion_id = t.suggestion_id
    WHERE t.status = 'ACTIVE'

    UNION

    -- Pending suggestions generated today
    SELECT DISTINCT
        sl.symbol      AS symbol,
        sl.expiry_date AS expiry_date,
        sl.strike      AS strike,
        sl.option_type AS option_type
    FROM options_suggestions  s
    JOIN options_suggestion_legs sl ON sl.suggestion_id = s.suggestion_id
    WHERE s.status = 'PENDING'
      AND CAST(s.generated_on AS DATE) = CAST(GETDATE() AS DATE)
    """

    def _loader() -> Iterator[OptionLegKey]:
        rows = db.fetch_all(sql)
        for r in rows:
            expiry = r["expiry_date"]
            if isinstance(expiry, datetime):
                expiry = expiry.date()
            yield (
                str(r["symbol"]),
                expiry,
                float(r["strike"]),
                str(r["option_type"]),
            )

    return _loader


def make_static_leg_loader(legs: Iterable[OptionLegKey]) -> LegLoader:
    """Trivial loader for tests / smoke runs."""
    snapshot: List[OptionLegKey] = list(legs)
    return lambda: list(snapshot)


# ---------------------------------------------------------------------------
# Watchlist loader — for the 5-min chain aggregator
# ---------------------------------------------------------------------------
def make_watchlist_leg_loader(
    instrument_master: InstrumentMaster,
    *,
    underlyings: Iterable[str],
    spot_lookup: Callable[[str], Optional[float]],
    band_pct: float = 0.05,
    expiries_per_underlying: int = 2,
) -> LegLoader:
    """Return a loader that yields option legs for the suggestion-engine watchlist.

    For each underlying it picks the next `expiries_per_underlying` upcoming
    expiries from `instrument_master`, then takes every CE/PE strike within
    ±`band_pct` of the current spot (resolved via `spot_lookup`). Used by
    `lifecycle/chain_aggregator.py` to build a chain trajectory without
    requiring an open trade or pending suggestion on each underlying.

    `spot_lookup(symbol)` returns the latest spot for the underlying, or
    None if not yet known. When None, the loader skips that underlying for
    this cycle (the next reconcile will pick it up once spot ticks arrive).
    """
    underlying_names: List[str] = [str(u).upper() for u in underlyings]

    def _loader() -> Iterator[OptionLegKey]:
        today = date.today()
        for sym in underlying_names:
            spot = spot_lookup(sym)
            if spot is None or spot <= 0:
                continue
            expiries = [
                e for e in instrument_master.list_expiries(sym) if e >= today
            ][:expiries_per_underlying]
            lo = spot * (1.0 - band_pct)
            hi = spot * (1.0 + band_pct)
            for exp in expiries:
                seen: set = set()
                for inst in instrument_master.list_options(sym, exp):
                    if inst.instrument_type not in ("CE", "PE"):
                        continue
                    if inst.strike < lo or inst.strike > hi:
                        continue
                    k = (sym, exp, float(inst.strike), inst.instrument_type)
                    if k in seen:
                        continue
                    seen.add(k)
                    yield k

    return _loader


def merge_leg_loaders(*loaders: LegLoader) -> LegLoader:
    """Concatenate multiple leg loaders into one. Order preserved; the
    SubscriptionManager dedupes via a set so duplicate keys are harmless."""
    snap: Tuple[LegLoader, ...] = tuple(loaders)

    def _loader() -> Iterator[OptionLegKey]:
        for ld in snap:
            yield from ld()

    return _loader
