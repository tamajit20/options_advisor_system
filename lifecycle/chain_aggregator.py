"""
lifecycle/chain_aggregator.py
=============================

5-minute boundary-aligned aggregator over the Zerodha WebSocket tick stream.

Subscribes to `TOPIC_TICK` on the in-process `EventBus`, maintains an
in-memory bucket per (symbol, expiry, strike, option_type) for the current
window, and on each 5-min boundary computes per-(symbol, expiry) chain
aggregates plus an ATM IV reading. Persists to:

    options_chain_5min   — chain-aggregate row per (symbol, expiry, snapshot_at)
    options_atm_iv_5min  — ATM IV row per (symbol, expiry, snapshot_at)

Used at live-suggestion time to derive trajectory metrics
(slope/persistence/acceleration of OI PCR + ATM IV) feeding the new
confidence gates and select_strategy bias.

Design
------
* Single thread for the boundary timer; tick callbacks run on the
  publisher's thread (same pattern as IntradayMonitor) but only update
  per-strike scalars under a lock — never block.
* Each bucket holds the *latest* tick observation in the window, plus the
  bucket's first-seen OI (so we can compute Δ vs window-start) and the
  bucket's volume floor (so the per-window volume is the latest minus the
  window-start volume_traded snapshot).
* On flush, we re-snapshot per-bucket "open OI / open volume" so the next
  window's deltas are relative to the just-flushed boundary, not session
  open.
* Idempotent: re-flushing the same boundary replaces the row (DELETE-then-
  INSERT semantics inside the repo).
* Failure-safe: any DB / IV-calc error is logged and swallowed; the timer
  keeps running.
* No business logic — purely captures and persists.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from config import STRATEGY_CONFIG
from contracts import ChainTrajectory
from database.connection import SQLServerConnection
from database.models import AtmIvTimeseriesRepo, ChainTimeseriesRepo
from engine.iv_calculator import implied_vol
from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK, get_event_bus
from utils import now_ist


logger = logging.getLogger(__name__)


BucketKey = Tuple[str, date, float, str]   # (symbol, expiry, strike, option_type)


@dataclass
class _StrikeBucket:
    last_price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    open_interest: Optional[int] = None        # latest tick OI
    open_interest_at_window_start: Optional[int] = None
    volume: Optional[int] = None               # latest cumulative volume
    volume_at_window_start: Optional[int] = None
    last_tick_at: Optional[datetime] = None
    sample_count: int = 0


@dataclass
class _SpotBucket:
    last_price: Optional[float] = None
    last_tick_at: Optional[datetime] = None


def _floor_to_5min(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


def _next_5min_boundary(dt: datetime) -> datetime:
    return _floor_to_5min(dt) + timedelta(minutes=5)


class ChainTickAggregator:
    """Subscribes to TOPIC_TICK, flushes 5-min aggregates to the DB.

    Parameters
    ----------
    db: SQLServerConnection used to construct the timeseries repos.
    expiry_provider: callable `(symbol) -> List[date]` returning the
        expiries we want to track for the underlying. Typically the next
        two upcoming weeklies/monthlies.
    risk_free_rate: passed to `implied_vol` for ATM IV computation.
    event_bus: defaults to the process singleton.
    clock: injectable for tests.
    """

    def __init__(
        self,
        *,
        db: SQLServerConnection,
        expiry_provider: Callable[[str], List[date]],
        risk_free_rate: Optional[float] = None,
        event_bus: Optional[EventBus] = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._db = db
        self._chain_repo = ChainTimeseriesRepo(db)
        self._iv_repo = AtmIvTimeseriesRepo(db)
        self._expiry_provider = expiry_provider
        self._rf = (
            float(risk_free_rate)
            if risk_free_rate is not None
            else float(STRATEGY_CONFIG["risk_free_rate"])
        )
        self._bus = event_bus or get_event_bus()
        self._clock = clock

        self._lock = threading.RLock()
        self._buckets: Dict[BucketKey, _StrikeBucket] = {}
        self._spots: Dict[str, _SpotBucket] = {}

        self._unsub: Optional[Callable[[], None]] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with self._lock:
            if self._unsub is not None:
                return
            self._unsub = self._bus.subscribe(TOPIC_TICK, self._on_tick)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="chain-aggregator-flush", daemon=True,
        )
        self._thread.start()
        logger.info("ChainTickAggregator: started")

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if self._unsub is not None:
                try:
                    self._unsub()
                finally:
                    self._unsub = None
        self._stop_event.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None
        logger.info("ChainTickAggregator: stopped")

    # ------------------------------------------------------------------ tick
    def _on_tick(self, q: LiveQuote) -> None:
        if q is None:
            return
        # Spot/index — keep only latest price for ATM-strike resolution.
        if q.option_type is None or q.expiry is None or q.strike is None:
            with self._lock:
                sb = self._spots.setdefault(q.symbol, _SpotBucket())
                sb.last_price = q.last_price
                sb.last_tick_at = q.timestamp or self._clock()
            return
        key: BucketKey = (q.symbol, q.expiry, float(q.strike), str(q.option_type))
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _StrikeBucket()
                self._buckets[key] = b
                # First tick in this window — snapshot starting OI/volume.
                if q.open_interest is not None:
                    b.open_interest_at_window_start = int(q.open_interest)
                if q.volume is not None:
                    b.volume_at_window_start = int(q.volume)
            b.last_price = q.last_price
            b.bid = q.bid
            b.ask = q.ask
            if q.open_interest is not None:
                b.open_interest = int(q.open_interest)
            if q.volume is not None:
                b.volume = int(q.volume)
            b.last_tick_at = q.timestamp or self._clock()
            b.sample_count += 1

    # ------------------------------------------------------------------ flush loop
    def _run(self) -> None:
        # Wait until the next 5-min boundary, then flush; repeat.
        while not self._stop_event.is_set():
            now = self._clock()
            target = _next_5min_boundary(now)
            wait_s = max((target - now).total_seconds(), 0.0)
            if self._stop_event.wait(wait_s):
                return
            try:
                self.flush_at(target)
            except Exception:
                logger.exception("ChainTickAggregator: flush failed")

    def flush_at(self, snapshot_at: datetime) -> None:
        """Snapshot current buckets to DB, scoped to the boundary `snapshot_at`.

        Public so tests / admin hooks can trigger a deterministic flush.
        """
        with self._lock:
            buckets_copy = {k: _StrikeBucket(**vars(b)) for k, b in self._buckets.items()}
            spots_copy = {s: _SpotBucket(**vars(v)) for s, v in self._spots.items()}

        # Group buckets by (symbol, expiry).
        groups: Dict[Tuple[str, date], Dict[BucketKey, _StrikeBucket]] = {}
        for key, b in buckets_copy.items():
            symbol, expiry, _strike, _ot = key
            groups.setdefault((symbol, expiry), {})[key] = b

        chain_rows: List[dict] = []
        iv_rows: List[dict] = []

        for (symbol, expiry), group in groups.items():
            spot_b = spots_copy.get(symbol)
            spot_val = spot_b.last_price if spot_b else None

            sum_call_oi = sum_put_oi = 0
            sum_call_oi_delta = sum_put_oi_delta = 0
            sum_call_vol = sum_put_vol = 0
            atm_strike = None
            atm_call_mid = atm_put_mid = None
            atm_call_spread_bps = atm_put_spread_bps = None
            sample_count = 0

            # Resolve ATM strike: nearest strike to spot.
            strikes = sorted({k[2] for k in group.keys()})
            if spot_val is not None and strikes:
                atm_strike = min(strikes, key=lambda s: abs(s - spot_val))

            for key, b in group.items():
                _sym, _exp, strike, ot = key
                sample_count += b.sample_count
                if b.open_interest is not None:
                    if ot == "CE":
                        sum_call_oi += b.open_interest
                    else:
                        sum_put_oi += b.open_interest
                if (
                    b.open_interest is not None
                    and b.open_interest_at_window_start is not None
                ):
                    delta = b.open_interest - b.open_interest_at_window_start
                    if ot == "CE":
                        sum_call_oi_delta += delta
                    else:
                        sum_put_oi_delta += delta
                if b.volume is not None and b.volume_at_window_start is not None:
                    vd = max(b.volume - b.volume_at_window_start, 0)
                    if ot == "CE":
                        sum_call_vol += vd
                    else:
                        sum_put_vol += vd
                if atm_strike is not None and strike == atm_strike:
                    mid = self._mid(b.bid, b.ask, b.last_price)
                    spr = self._spread_bps(b.bid, b.ask)
                    if ot == "CE":
                        atm_call_mid = mid
                        atm_call_spread_bps = spr
                    else:
                        atm_put_mid = mid
                        atm_put_spread_bps = spr

            chain_rows.append({
                "snapshot_at": snapshot_at,
                "symbol": symbol,
                "expiry_date": expiry,
                "spot": spot_val,
                "atm_strike": atm_strike,
                "sum_call_oi": sum_call_oi or None,
                "sum_put_oi": sum_put_oi or None,
                "sum_call_oi_delta": sum_call_oi_delta if (sum_call_oi or sum_put_oi) else None,
                "sum_put_oi_delta": sum_put_oi_delta if (sum_call_oi or sum_put_oi) else None,
                "sum_call_volume": sum_call_vol or None,
                "sum_put_volume": sum_put_vol or None,
                "atm_call_mid": atm_call_mid,
                "atm_put_mid": atm_put_mid,
                "atm_call_spread_bps": atm_call_spread_bps,
                "atm_put_spread_bps": atm_put_spread_bps,
                "sample_count": sample_count or None,
            })

            # ATM IV — average of CE and PE bisection results when both available.
            if (
                spot_val is not None and atm_strike is not None
                and (atm_call_mid is not None or atm_put_mid is not None)
            ):
                dte = max((expiry - snapshot_at.date()).days, 0)
                ivs: List[float] = []
                if atm_call_mid is not None and atm_call_mid > 0:
                    iv_ce, ok = implied_vol(
                        atm_call_mid, spot_val, atm_strike, dte, "CE", self._rf,
                    )
                    if ok and iv_ce > 0:
                        ivs.append(iv_ce)
                if atm_put_mid is not None and atm_put_mid > 0:
                    iv_pe, ok = implied_vol(
                        atm_put_mid, spot_val, atm_strike, dte, "PE", self._rf,
                    )
                    if ok and iv_pe > 0:
                        ivs.append(iv_pe)
                if ivs:
                    iv_rows.append({
                        "snapshot_at": snapshot_at,
                        "symbol": symbol,
                        "expiry_date": expiry,
                        "atm_strike": atm_strike,
                        "spot": spot_val,
                        "dte": dte,
                        "atm_iv": sum(ivs) / len(ivs),
                    })

        if chain_rows:
            try:
                self._chain_repo.insert_many(chain_rows)
            except Exception:
                logger.exception("ChainTickAggregator: chain insert failed")
        if iv_rows:
            try:
                self._iv_repo.insert_many(iv_rows)
            except Exception:
                logger.exception("ChainTickAggregator: ATM IV insert failed")
        # Commit so rows survive a process/container restart. Without this
        # every flush rides on one ever-growing transaction that SQL Server
        # rolls back when the connection drops, leaving the 5-min tables
        # mysteriously empty after every redeploy.
        if chain_rows or iv_rows:
            try:
                self._db.commit()
            except Exception:
                logger.exception("ChainTickAggregator: commit failed")

        # Re-baseline bucket window-start counters so the next window's deltas
        # are relative to the boundary we just flushed, not session open.
        with self._lock:
            for key, b in self._buckets.items():
                if b.open_interest is not None:
                    b.open_interest_at_window_start = b.open_interest
                if b.volume is not None:
                    b.volume_at_window_start = b.volume
                b.sample_count = 0

        if chain_rows or iv_rows:
            logger.info(
                "ChainTickAggregator: flushed %d chain row(s), %d IV row(s) at %s",
                len(chain_rows), len(iv_rows), snapshot_at,
            )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _mid(bid: Optional[float], ask: Optional[float], ltp: Optional[float]) -> Optional[float]:
        if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
            return (bid + ask) / 2.0
        if ltp is not None and ltp > 0:
            return float(ltp)
        return None

    @staticmethod
    def _spread_bps(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None
        return ((ask - bid) / mid) * 10_000.0


# ---------------------------------------------------------------------------
# Trajectory loader (used by suggestion_engine)
# ---------------------------------------------------------------------------

def load_trajectory(
    db: SQLServerConnection,
    *,
    symbol: str,
    expiry: date,
    now: Optional[datetime] = None,
    window_minutes: int = 60,
    max_samples: int = 12,
) -> ChainTrajectory:
    """Read recent 5-min snapshot rows and assemble a `ChainTrajectory`.

    Returns an empty trajectory when there are no recent rows.
    """
    chain_repo = ChainTimeseriesRepo(db)
    iv_repo = AtmIvTimeseriesRepo(db)
    ts = now or now_ist()
    since = ts - timedelta(minutes=window_minutes)

    chain_rows = chain_repo.recent_window(symbol, expiry, since, limit=max_samples)
    iv_rows = iv_repo.recent_window(symbol, expiry, since, limit=max_samples)

    # OI PCR change series: ΣΔPut / ΣΔCall per row.
    oi_pcr_series: List[Optional[float]] = []
    call_vol_series: List[Optional[float]] = []
    put_vol_series: List[Optional[float]] = []
    for r in chain_rows:
        cd = r.get("sum_call_oi_delta")
        pd = r.get("sum_put_oi_delta")
        if cd is not None and pd is not None and cd > 0:
            oi_pcr_series.append(float(pd) / float(cd))
        else:
            oi_pcr_series.append(None)
        cv = r.get("sum_call_volume")
        pv = r.get("sum_put_volume")
        call_vol_series.append(float(cv) if cv is not None else None)
        put_vol_series.append(float(pv) if pv is not None else None)

    iv_series: List[Optional[float]] = [
        float(r["atm_iv"]) if r.get("atm_iv") is not None else None for r in iv_rows
    ]

    latest_call_spread = latest_put_spread = None
    if chain_rows:
        last = chain_rows[-1]
        if last.get("atm_call_spread_bps") is not None:
            latest_call_spread = float(last["atm_call_spread_bps"])
        if last.get("atm_put_spread_bps") is not None:
            latest_put_spread = float(last["atm_put_spread_bps"])

    return ChainTrajectory(
        oi_pcr_change_series=oi_pcr_series,
        atm_iv_series=iv_series,
        call_volume_series=call_vol_series,
        put_volume_series=put_vol_series,
        latest_call_spread_bps=latest_call_spread,
        latest_put_spread_bps=latest_put_spread,
    )
