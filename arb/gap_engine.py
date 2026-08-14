"""
arb/gap_engine.py — fast in-memory NSE↔BSE gap episode tracker.

Subscribes to ``tick.arb``, pairs legs in memory, and flushes episodes to SQL
via a background writer thread (minimal per-tick DB work).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, Optional

from config import ARB_CONFIG
from database.arb_models import ArbGapRepo
from database.connection import SQLServerConnection
from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK_ARB, get_event_bus
from utils import now_ist

logger = logging.getLogger(__name__)


class _DbOp(str, Enum):
    INSERT = "insert"
    UPDATE = "update"
    CLOSE = "close"


@dataclass
class _LegSnapshot:
    ltp: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_qty: Optional[int] = None
    ask_qty: Optional[int] = None
    updated_at: Optional[datetime] = None


@dataclass
class _OpenEpisode:
    db_id: Optional[int] = None
    symbol: str = ""
    isin: Optional[str] = None
    started_at: Optional[datetime] = None
    direction: str = ""
    max_gap_pct: float = 0.0
    sample_count: int = 0
    last_payload: dict = field(default_factory=dict)
    dirty: bool = False


def compute_gap(nse_ltp: float, bse_ltp: float) -> tuple[float, float, str]:
    """Return (gap_abs, gap_pct, direction)."""
    gap_abs = round(nse_ltp - bse_ltp, 4)
    base = min(nse_ltp, bse_ltp)
    if base <= 0:
        gap_pct = 0.0
    else:
        gap_pct = round(abs(gap_abs) / base * 100.0, 4)
    if gap_abs > 0:
        direction = "NSE_HIGH"
    elif gap_abs < 0:
        direction = "BSE_HIGH"
    else:
        direction = "FLAT"
    return gap_abs, gap_pct, direction


def _quote_to_leg(quote: LiveQuote) -> _LegSnapshot:
    return _LegSnapshot(
        ltp=float(quote.last_price or 0),
        bid=quote.bid,
        ask=quote.ask,
        bid_qty=getattr(quote, "bid_qty", None),
        ask_qty=getattr(quote, "ask_qty", None),
        updated_at=quote.timestamp or now_ist(),
    )


def _episode_payload(
    *,
    symbol: str,
    isin: Optional[str],
    started_at: datetime,
    now: datetime,
    nse: _LegSnapshot,
    bse: _LegSnapshot,
    gap_abs: float,
    gap_pct: float,
    direction: str,
    max_gap_pct: float,
    sample_count: int,
) -> dict:
    duration = max(int((now - started_at).total_seconds()), 0)
    return {
        "symbol": symbol,
        "isin": isin,
        "started_at": started_at,
        "duration_sec": duration,
        "nse_ltp": nse.ltp,
        "bse_ltp": bse.ltp,
        "gap_abs": gap_abs,
        "gap_pct": gap_pct,
        "direction": direction,
        "nse_bid": nse.bid,
        "nse_ask": nse.ask,
        "nse_bid_qty": nse.bid_qty,
        "nse_ask_qty": nse.ask_qty,
        "bse_bid": bse.bid,
        "bse_ask": bse.ask,
        "bse_bid_qty": bse.bid_qty,
        "bse_ask_qty": bse.ask_qty,
        "max_gap_pct": max_gap_pct,
        "sample_count": sample_count,
    }


class ArbGapEngine:
    """Subscribe to arb ticks; track gap episodes with async DB persistence."""

    def __init__(
        self,
        *,
        db: SQLServerConnection,
        event_bus: Optional[EventBus] = None,
        isin_lookup: Optional[Callable[[str], Optional[str]]] = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._db = db
        self._gap_repo = ArbGapRepo(db)
        self._bus = event_bus or get_event_bus()
        self._isin_lookup = isin_lookup or (lambda _s: None)
        self._clock = clock

        self._pair_staleness = float(ARB_CONFIG.get("tick_staleness_sec", 3))
        self._close_staleness = float(ARB_CONFIG.get("leg_stale_close_sec", 5))
        self._flush_interval = float(ARB_CONFIG.get("db_flush_interval_sec", 1))
        self._live_state_path = str(ARB_CONFIG.get("live_state_path") or "data/arb_live_state.json")

        self._lock = threading.RLock()
        self._nse: Dict[str, _LegSnapshot] = {}
        self._bse: Dict[str, _LegSnapshot] = {}
        self._open: Dict[str, _OpenEpisode] = {}
        self._live: Dict[str, dict] = {}
        self._pending_insert_close: Dict[str, tuple] = {}

        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self._stale_thread: Optional[threading.Thread] = None
        self._unsubscribe: Optional[Callable[[], None]] = None

    def start(self) -> None:
        if not ARB_CONFIG.get("enabled", True):
            logger.info("ArbGapEngine disabled via ARB_CONFIG")
            return
        self._unsubscribe = self._bus.subscribe(TOPIC_TICK_ARB, self._on_tick)
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="arb-gap-db-writer", daemon=True)
        self._writer.start()
        self._stale_thread = threading.Thread(target=self._stale_loop, name="arb-gap-stale", daemon=True)
        self._stale_thread.start()
        logger.info("ArbGapEngine started")

    def stop(self) -> None:
        self._stop.set()
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        for t in (self._writer, self._stale_thread):
            if t is not None:
                t.join(timeout=3.0)
        self._writer = None
        self._stale_thread = None
        self._flush_pending(force=True)

    def live_gaps(self) -> list:
        with self._lock:
            return list(self._live.values())

    def live_snapshot(self) -> dict:
        with self._lock:
            gaps = list(self._live.values())
        return {"gaps": gaps, "count": len(gaps), "source": "engine"}

    def _publish_live_state(self, now: Optional[datetime] = None) -> None:
        """Write open gaps to a shared file for the dashboard SSE stream."""
        ts = now or self._clock()
        with self._lock:
            gaps = list(self._live.values())
        snap = {
            "gaps": gaps,
            "count": len(gaps),
            "source": "engine",
            "as_of": ts.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            path = self._live_state_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            os.replace(tmp, path)
        except Exception:
            logger.debug("arb live state write failed", exc_info=True)

    def _on_tick(self, quote: LiveQuote) -> None:
        if quote is None or quote.option_type is not None:
            return
        exchange = (getattr(quote, "exchange", None) or "").upper()
        symbol = str(quote.symbol or "").upper()
        if not symbol or exchange not in ("NSE", "BSE"):
            return
        leg = _quote_to_leg(quote)
        with self._lock:
            if exchange == "NSE":
                self._nse[symbol] = leg
            else:
                self._bse[symbol] = leg
            self._evaluate_symbol(symbol)

    def _leg_fresh(self, leg: Optional[_LegSnapshot], now: datetime) -> bool:
        if leg is None or leg.updated_at is None or leg.ltp <= 0:
            return False
        age = (now - leg.updated_at).total_seconds()
        return age <= self._pair_staleness

    def _evaluate_symbol(self, symbol: str) -> None:
        now = self._clock()
        nse = self._nse.get(symbol)
        bse = self._bse.get(symbol)
        if not self._leg_fresh(nse, now) or not self._leg_fresh(bse, now):
            return

        gap_abs, gap_pct, direction = compute_gap(nse.ltp, bse.ltp)  # type: ignore[union-attr]
        isin = self._isin_lookup(symbol)
        ep = self._open.get(symbol)

        if gap_abs == 0:
            if ep is not None:
                self._close_episode(symbol, ep, now, nse, bse, gap_abs, gap_pct, direction)  # type: ignore[arg-type]
            elif symbol in self._live:
                self._live.pop(symbol, None)
                self._publish_live_state(now)
            return

        if ep is None:
            self._start_episode(symbol, isin, now, nse, bse, gap_abs, gap_pct, direction)  # type: ignore[arg-type]
            return

        if ep.direction and direction != ep.direction and direction != "FLAT" and ep.direction != "FLAT":
            self._close_episode(symbol, ep, now, nse, bse, gap_abs, gap_pct, direction)  # type: ignore[arg-type]
            self._start_episode(symbol, isin, now, nse, bse, gap_abs, gap_pct, direction)  # type: ignore[arg-type]
            return

        ep.sample_count += 1
        ep.max_gap_pct = max(ep.max_gap_pct, gap_pct)
        ep.direction = direction
        payload = _episode_payload(
            symbol=symbol,
            isin=isin or ep.isin,
            started_at=ep.started_at or now,  # type: ignore[arg-type]
            now=now,
            nse=nse,  # type: ignore[arg-type]
            bse=bse,  # type: ignore[arg-type]
            gap_abs=gap_abs,
            gap_pct=gap_pct,
            direction=direction,
            max_gap_pct=ep.max_gap_pct,
            sample_count=ep.sample_count,
        )
        ep.last_payload = payload
        ep.dirty = True
        self._live[symbol] = {**payload, "started_at": payload["started_at"].isoformat(sep=" ", timespec="seconds")}
        self._publish_live_state(now)

    def _start_episode(
        self,
        symbol: str,
        isin: Optional[str],
        now: datetime,
        nse: _LegSnapshot,
        bse: _LegSnapshot,
        gap_abs: float,
        gap_pct: float,
        direction: str,
    ) -> None:
        payload = _episode_payload(
            symbol=symbol,
            isin=isin,
            started_at=now,
            now=now,
            nse=nse,
            bse=bse,
            gap_abs=gap_abs,
            gap_pct=gap_pct,
            direction=direction,
            max_gap_pct=gap_pct,
            sample_count=1,
        )
        ep = _OpenEpisode(
            db_id=None,
            symbol=symbol,
            isin=isin,
            started_at=now,
            direction=direction,
            max_gap_pct=gap_pct,
            sample_count=1,
            last_payload=payload,
            dirty=True,
        )
        self._open[symbol] = ep
        self._live[symbol] = {**payload, "started_at": now.isoformat(sep=" ", timespec="seconds")}
        self._q.put((_DbOp.INSERT, symbol, payload))
        self._publish_live_state(now)

    def _close_episode(
        self,
        symbol: str,
        ep: _OpenEpisode,
        now: datetime,
        nse: _LegSnapshot,
        bse: _LegSnapshot,
        gap_abs: float,
        gap_pct: float,
        direction: str,
    ) -> None:
        started = ep.started_at or now
        duration = max(int((now - started).total_seconds()), 0)
        payload = _episode_payload(
            symbol=symbol,
            isin=ep.isin,
            started_at=started,
            now=now,
            nse=nse,
            bse=bse,
            gap_abs=gap_abs,
            gap_pct=gap_pct,
            direction=direction or ep.direction,
            max_gap_pct=ep.max_gap_pct,
            sample_count=ep.sample_count,
        )
        self._open.pop(symbol, None)
        self._live.pop(symbol, None)
        self._publish_live_state(now)
        close_item = (now, duration, payload)
        if ep.db_id is not None:
            self._q.put((_DbOp.CLOSE, ep.db_id, now, duration, payload))
        else:
            self._pending_insert_close[symbol] = close_item
            self._q.put((_DbOp.INSERT, symbol, payload))

    def _stale_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            now = self._clock()
            with self._lock:
                for symbol in list(self._open.keys()):
                    nse = self._nse.get(symbol)
                    bse = self._bse.get(symbol)
                    stale = False
                    for leg in (nse, bse):
                        if leg is None or leg.updated_at is None:
                            stale = True
                            break
                        if (now - leg.updated_at).total_seconds() > self._close_staleness:
                            stale = True
                            break
                    if stale:
                        ep = self._open.get(symbol)
                        if ep is not None:
                            gap_abs, gap_pct, direction = 0.0, 0.0, "FLAT"
                            if nse and bse and nse.ltp > 0 and bse.ltp > 0:
                                gap_abs, gap_pct, direction = compute_gap(nse.ltp, bse.ltp)
                            self._close_episode(
                                symbol, ep, now,
                                nse or _LegSnapshot(), bse or _LegSnapshot(),
                                gap_abs, gap_pct, direction,
                            )
                self._flush_dirty_locked(now)

    def _flush_dirty_locked(self, now: datetime) -> None:
        for symbol, ep in list(self._open.items()):
            if not ep.dirty or ep.db_id is None:
                continue
            ep.dirty = False
            self._q.put((_DbOp.UPDATE, ep.db_id, dict(ep.last_payload)))

    def _flush_pending(self, *, force: bool = False) -> None:
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            self._apply_db_op(item)

    def _writer_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            self._flush_pending()
            with self._lock:
                self._flush_dirty_locked(self._clock())
        self._flush_pending(force=True)

    def _apply_db_op(self, item) -> None:
        try:
            op = item[0]
            if op == _DbOp.INSERT:
                _, symbol, payload = item
                gap_id = self._gap_repo.insert_open(payload)
                with self._lock:
                    pending = self._pending_insert_close.pop(symbol, None)
                    ep = self._open.get(symbol)
                    if ep is not None and ep.db_id is None:
                        ep.db_id = gap_id
                self._db.commit()
                if pending is not None:
                    ended_at, duration, close_payload = pending
                    self._q.put((_DbOp.CLOSE, gap_id, ended_at, duration, close_payload))
            elif op == _DbOp.UPDATE:
                _, gap_id, payload = item
                self._gap_repo.update_open(int(gap_id), payload)
                self._db.commit()
            elif op == _DbOp.CLOSE:
                _, gap_id, ended_at, duration, payload = item
                self._gap_repo.close(int(gap_id), ended_at=ended_at, duration_sec=int(duration), payload=payload)
                self._db.commit()
        except Exception:
            logger.exception("arb gap DB write failed")
            try:
                self._db.rollback()
            except Exception:
                pass
