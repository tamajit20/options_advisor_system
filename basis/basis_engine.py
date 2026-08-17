"""
basis/basis_engine.py — fast in-memory cash-futures basis episode tracker.

Subscribes to ``tick.basis``, pairs spot/fut legs in memory, and flushes
episodes to SQL via a background writer thread.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Callable, Dict, Optional

from config import BASIS_CONFIG
from basis.config_loader import get_basis_settings
from basis.settings_schema import basis_enabled
from database.basis_models import BasisEpisodeRepo
from database.connection import SQLServerConnection
from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK_BASIS, get_event_bus
from utils import now_ist

logger = logging.getLogger(__name__)


def _coerce_date(raw: object) -> Optional[date]:
    """Normalize DB/API expiry values to ``date``."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    if hasattr(raw, "date"):
        try:
            return raw.date()  # type: ignore[union-attr]
        except Exception:
            return None
    return None


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
    expiry: Optional[date] = None


@dataclass
class _OpenEpisode:
    db_id: Optional[int] = None
    symbol: str = ""
    fut_expiry: Optional[date] = None
    started_at: Optional[datetime] = None
    direction: str = ""
    max_basis_pct: float = 0.0
    sample_count: int = 0
    last_payload: dict = field(default_factory=dict)
    dirty: bool = False


def compute_basis(
    spot: float,
    fut: float,
    expiry: date,
    *,
    as_of: Optional[date] = None,
) -> tuple[float, float, float, str]:
    """Return (basis_abs, basis_pct, annualized_pct, direction)."""
    expiry_date = _coerce_date(expiry)
    if expiry_date is None:
        raise ValueError(f"invalid fut expiry: {expiry!r}")
    basis_abs = round(fut - spot, 4)
    if spot <= 0:
        basis_pct = 0.0
    else:
        basis_pct = round(basis_abs / spot * 100.0, 4)
    today = as_of or date.today()
    dte = max((expiry_date - today).days, 1)
    annualized_pct = round(basis_pct * 365 / dte, 4)
    if basis_abs > 0:
        direction = "CONTANGO"
    elif basis_abs < 0:
        direction = "BACKWARDATION"
    else:
        direction = "FLAT"
    return basis_abs, basis_pct, annualized_pct, direction


def _quote_to_leg(quote: LiveQuote) -> _LegSnapshot:
    exp = _coerce_date(quote.expiry)
    return _LegSnapshot(
        ltp=float(quote.last_price or 0),
        bid=quote.bid,
        ask=quote.ask,
        bid_qty=getattr(quote, "bid_qty", None),
        ask_qty=getattr(quote, "ask_qty", None),
        updated_at=quote.timestamp or now_ist(),
        expiry=exp,
    )


def _episode_payload(
    *,
    symbol: str,
    fut_expiry: Optional[date],
    started_at: datetime,
    now: datetime,
    spot: _LegSnapshot,
    fut: _LegSnapshot,
    basis_abs: float,
    basis_pct: float,
    annualized_pct: float,
    direction: str,
    max_basis_pct: float,
    sample_count: int,
) -> dict:
    duration = max(int((now - started_at).total_seconds()), 0)
    return {
        "symbol": symbol,
        "fut_expiry": fut_expiry,
        "started_at": started_at,
        "duration_sec": duration,
        "spot_ltp": spot.ltp,
        "fut_ltp": fut.ltp,
        "basis_abs": basis_abs,
        "basis_pct": basis_pct,
        "annualized_pct": annualized_pct,
        "direction": direction,
        "spot_bid": spot.bid,
        "spot_ask": spot.ask,
        "spot_bid_qty": spot.bid_qty,
        "spot_ask_qty": spot.ask_qty,
        "fut_bid": fut.bid,
        "fut_ask": fut.ask,
        "fut_bid_qty": fut.bid_qty,
        "fut_ask_qty": fut.ask_qty,
        "max_basis_pct": max_basis_pct,
        "sample_count": sample_count,
    }


class BasisEngine:
    """Subscribe to basis ticks; track basis episodes with async DB persistence."""

    def __init__(
        self,
        *,
        db: SQLServerConnection,
        event_bus: Optional[EventBus] = None,
        expiry_lookup: Optional[Callable[[str], Optional[date]]] = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._db = db
        self._episode_repo = BasisEpisodeRepo(db)
        self._bus = event_bus or get_event_bus()
        self._expiry_lookup = expiry_lookup or (lambda _s: None)
        self._clock = clock

        self._flush_interval = float(BASIS_CONFIG.get("db_flush_interval_sec", 1))
        self._live_state_path = str(BASIS_CONFIG.get("live_state_path") or "data/basis_live_state.json")
        self._reload_runtime_settings()

        self._lock = threading.RLock()
        self._spot: Dict[str, _LegSnapshot] = {}
        self._fut: Dict[str, _LegSnapshot] = {}
        self._open: Dict[str, _OpenEpisode] = {}
        self._live: Dict[str, dict] = {}
        self._pending_insert_close: Dict[str, tuple] = {}

        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self._stale_thread: Optional[threading.Thread] = None
        self._unsubscribe: Optional[Callable[[], None]] = None

    def _reload_runtime_settings(self) -> None:
        s = get_basis_settings(self._db, use_cache=False)
        self._pair_staleness = float(s.get("tick_staleness_sec", 3))
        self._close_staleness = float(BASIS_CONFIG.get("leg_stale_close_sec", 5))
        self._min_basis_store_pct = float(s.get("min_basis_store_pct", 0) or 0)
        self._min_duration_store_sec = int(s.get("min_duration_store_sec", 0) or 0)

    def start(self) -> None:
        settings = get_basis_settings(self._db)
        if not basis_enabled(settings):
            logger.info("BasisEngine disabled via settings")
            return
        self._unsubscribe = self._bus.subscribe(TOPIC_TICK_BASIS, self._on_tick)
        self._stop.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="basis-db-writer", daemon=True)
        self._writer.start()
        self._stale_thread = threading.Thread(target=self._stale_loop, name="basis-stale", daemon=True)
        self._stale_thread.start()
        logger.info("BasisEngine started")

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

    def live_basis(self) -> list:
        with self._lock:
            return list(self._live.values())

    def live_snapshot(self) -> dict:
        with self._lock:
            rows = list(self._live.values())
        return {"basis": rows, "count": len(rows), "source": "engine"}

    def _store_filter_enabled(self) -> bool:
        return self._min_basis_store_pct > 0 or self._min_duration_store_sec > 0

    def _basis_pct_meets_store(self, basis_pct: float) -> bool:
        if self._min_basis_store_pct <= 0:
            return True
        return abs(float(basis_pct)) >= self._min_basis_store_pct

    def _episode_meets_store(self, ep: _OpenEpisode, *, duration_sec: int) -> bool:
        if not self._store_filter_enabled():
            return True
        if self._min_basis_store_pct > 0 and ep.max_basis_pct < self._min_basis_store_pct:
            return False
        if self._min_duration_store_sec > 0 and duration_sec < self._min_duration_store_sec:
            return False
        return True

    def _maybe_queue_insert(self, symbol: str, ep: _OpenEpisode) -> None:
        if ep.db_id is not None:
            return
        if not self._basis_pct_meets_store(ep.max_basis_pct):
            return
        self._q.put((_DbOp.INSERT, symbol, dict(ep.last_payload)))

    def _publish_live_state(self, now: Optional[datetime] = None) -> None:
        ts = now or self._clock()
        with self._lock:
            rows = list(self._live.values())
        snap = {
            "basis": rows,
            "count": len(rows),
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
            logger.debug("basis live state write failed", exc_info=True)

    def _on_tick(self, quote: LiveQuote) -> None:
        if quote is None or quote.option_type is not None:
            return
        exchange = (getattr(quote, "exchange", None) or "").upper()
        symbol = str(quote.symbol or "").upper()
        if not symbol or exchange not in ("NSE", "NFO"):
            return
        leg = _quote_to_leg(quote)
        with self._lock:
            if exchange == "NSE":
                self._spot[symbol] = leg
            else:
                self._fut[symbol] = leg
            self._evaluate_symbol(symbol)

    def _leg_fresh(self, leg: Optional[_LegSnapshot], now: datetime) -> bool:
        if leg is None or leg.updated_at is None or leg.ltp <= 0:
            return False
        age = (now - leg.updated_at).total_seconds()
        return age <= self._pair_staleness

    def _resolve_expiry(self, symbol: str, spot: _LegSnapshot, fut: _LegSnapshot) -> Optional[date]:
        for leg in (fut, spot):
            exp = _coerce_date(leg.expiry)
            if exp is not None:
                return exp
        ep = self._open.get(symbol)
        if ep and ep.fut_expiry:
            return _coerce_date(ep.fut_expiry)
        return _coerce_date(self._expiry_lookup(symbol))

    def _evaluate_symbol(self, symbol: str) -> None:
        now = self._clock()
        spot = self._spot.get(symbol)
        fut = self._fut.get(symbol)
        if not self._leg_fresh(spot, now) or not self._leg_fresh(fut, now):
            return

        fut_expiry = self._resolve_expiry(symbol, spot, fut)  # type: ignore[arg-type]
        if fut_expiry is None:
            return

        basis_abs, basis_pct, annualized_pct, direction = compute_basis(
            spot.ltp, fut.ltp, fut_expiry, as_of=now.date(),  # type: ignore[union-attr]
        )
        ep = self._open.get(symbol)
        abs_pct = abs(basis_pct)

        if basis_abs == 0:
            if ep is not None:
                self._close_episode(
                    symbol, ep, now, spot, fut, fut_expiry,  # type: ignore[arg-type]
                    basis_abs, basis_pct, annualized_pct, direction,
                )
            elif symbol in self._live:
                self._live.pop(symbol, None)
                self._publish_live_state(now)
            return

        if ep is None:
            self._start_episode(
                symbol, fut_expiry, now, spot, fut,  # type: ignore[arg-type]
                basis_abs, basis_pct, annualized_pct, direction, abs_pct,
            )
            return

        if ep.direction and direction != ep.direction and direction != "FLAT" and ep.direction != "FLAT":
            self._close_episode(
                symbol, ep, now, spot, fut, fut_expiry,  # type: ignore[arg-type]
                basis_abs, basis_pct, annualized_pct, direction,
            )
            self._start_episode(
                symbol, fut_expiry, now, spot, fut,  # type: ignore[arg-type]
                basis_abs, basis_pct, annualized_pct, direction, abs_pct,
            )
            return

        ep.sample_count += 1
        ep.max_basis_pct = max(ep.max_basis_pct, abs_pct)
        ep.direction = direction
        ep.fut_expiry = fut_expiry
        payload = _episode_payload(
            symbol=symbol,
            fut_expiry=fut_expiry,
            started_at=ep.started_at or now,
            now=now,
            spot=spot,  # type: ignore[arg-type]
            fut=fut,  # type: ignore[arg-type]
            basis_abs=basis_abs,
            basis_pct=basis_pct,
            annualized_pct=annualized_pct,
            direction=direction,
            max_basis_pct=ep.max_basis_pct,
            sample_count=ep.sample_count,
        )
        ep.last_payload = payload
        ep.dirty = True
        self._live[symbol] = self._live_row(payload)
        self._maybe_queue_insert(symbol, ep)
        self._publish_live_state(now)

    def _live_row(self, payload: dict) -> dict:
        row = {**payload}
        started = payload.get("started_at")
        if isinstance(started, datetime):
            row["started_at"] = started.isoformat(sep=" ", timespec="seconds")
        exp = payload.get("fut_expiry")
        if isinstance(exp, date):
            row["fut_expiry"] = exp.isoformat()
        return row

    def _start_episode(
        self,
        symbol: str,
        fut_expiry: date,
        now: datetime,
        spot: _LegSnapshot,
        fut: _LegSnapshot,
        basis_abs: float,
        basis_pct: float,
        annualized_pct: float,
        direction: str,
        abs_pct: float,
    ) -> None:
        payload = _episode_payload(
            symbol=symbol,
            fut_expiry=fut_expiry,
            started_at=now,
            now=now,
            spot=spot,
            fut=fut,
            basis_abs=basis_abs,
            basis_pct=basis_pct,
            annualized_pct=annualized_pct,
            direction=direction,
            max_basis_pct=abs_pct,
            sample_count=1,
        )
        ep = _OpenEpisode(
            db_id=None,
            symbol=symbol,
            fut_expiry=fut_expiry,
            started_at=now,
            direction=direction,
            max_basis_pct=abs_pct,
            sample_count=1,
            last_payload=payload,
            dirty=True,
        )
        self._open[symbol] = ep
        self._live[symbol] = self._live_row(payload)
        self._maybe_queue_insert(symbol, ep)
        self._publish_live_state(now)

    def _close_episode(
        self,
        symbol: str,
        ep: _OpenEpisode,
        now: datetime,
        spot: _LegSnapshot,
        fut: _LegSnapshot,
        fut_expiry: date,
        basis_abs: float,
        basis_pct: float,
        annualized_pct: float,
        direction: str,
    ) -> None:
        started = ep.started_at or now
        duration = max(int((now - started).total_seconds()), 0)
        abs_pct = abs(basis_pct)
        ep.max_basis_pct = max(ep.max_basis_pct, abs_pct)
        payload = _episode_payload(
            symbol=symbol,
            fut_expiry=fut_expiry,
            started_at=started,
            now=now,
            spot=spot,
            fut=fut,
            basis_abs=basis_abs,
            basis_pct=basis_pct,
            annualized_pct=annualized_pct,
            direction=direction or ep.direction,
            max_basis_pct=ep.max_basis_pct,
            sample_count=ep.sample_count,
        )
        self._open.pop(symbol, None)
        self._live.pop(symbol, None)
        self._publish_live_state(now)
        if not self._episode_meets_store(ep, duration_sec=duration):
            return
        close_item = (now, duration, payload)
        if ep.db_id is not None:
            self._q.put((_DbOp.CLOSE, ep.db_id, now, duration, payload))
        else:
            self._pending_insert_close[symbol] = close_item
            self._q.put((_DbOp.INSERT, symbol, payload))

    def _stale_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            self._reload_runtime_settings()
            now = self._clock()
            with self._lock:
                for symbol in list(self._open.keys()):
                    spot = self._spot.get(symbol)
                    fut = self._fut.get(symbol)
                    stale = False
                    for leg in (spot, fut):
                        if leg is None or leg.updated_at is None:
                            stale = True
                            break
                        if (now - leg.updated_at).total_seconds() > self._close_staleness:
                            stale = True
                            break
                    if stale:
                        ep = self._open.get(symbol)
                        if ep is not None:
                            fut_expiry = self._resolve_expiry(symbol, spot or _LegSnapshot(), fut or _LegSnapshot())
                            if fut_expiry is None:
                                fut_expiry = ep.fut_expiry or now.date()
                            basis_abs, basis_pct, annualized_pct, direction = 0.0, 0.0, 0.0, "FLAT"
                            if spot and fut and spot.ltp > 0 and fut.ltp > 0:
                                basis_abs, basis_pct, annualized_pct, direction = compute_basis(
                                    spot.ltp, fut.ltp, fut_expiry, as_of=now.date(),
                                )
                            self._close_episode(
                                symbol, ep, now,
                                spot or _LegSnapshot(), fut or _LegSnapshot(), fut_expiry,
                                basis_abs, basis_pct, annualized_pct, direction,
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
            self._reload_runtime_settings()
            self._flush_pending()
            with self._lock:
                self._flush_dirty_locked(self._clock())
        self._flush_pending(force=True)

    def _apply_db_op(self, item) -> None:
        try:
            op = item[0]
            if op == _DbOp.INSERT:
                _, symbol, payload = item
                ep_id = self._episode_repo.insert_open(payload)
                with self._lock:
                    pending = self._pending_insert_close.pop(symbol, None)
                    ep = self._open.get(symbol)
                    if ep is not None and ep.db_id is None:
                        ep.db_id = ep_id
                self._db.commit()
                if pending is not None:
                    ended_at, duration, close_payload = pending
                    self._q.put((_DbOp.CLOSE, ep_id, ended_at, duration, close_payload))
            elif op == _DbOp.UPDATE:
                _, ep_id, payload = item
                self._episode_repo.update_open(int(ep_id), payload)
                self._db.commit()
            elif op == _DbOp.CLOSE:
                _, ep_id, ended_at, duration, payload = item
                self._episode_repo.close(
                    int(ep_id), ended_at=ended_at, duration_sec=int(duration), payload=payload,
                )
                self._db.commit()
        except Exception:
            logger.exception("basis episode DB write failed")
            try:
                self._db.rollback()
            except Exception:
                pass
