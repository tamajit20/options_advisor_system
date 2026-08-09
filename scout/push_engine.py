"""
scout/push_engine.py — push-based scout signals from Zerodha WebSocket ticks.

Builds live 1-minute OHLCV bars per watchlist equity, evaluates patterns on
each bar close (~1s after the minute boundary), and persists signals to
scout_signals. REST historical_data seeds today's bars at startup so OR-style
rules work immediately after the WS runner connects.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Set, Tuple

from config import SCOUT_CONFIG
from database.connection import SQLServerConnection
from database.scout_models import ScoutSignalRepo
from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK, get_event_bus
from scout.candles import Candle
from scout.filters import min_candles_ok
from scout.patterns import detect_signals
from scout.utils import is_market_open, pct_change
from utils import now_ist

logger = logging.getLogger(__name__)

_INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "VIX"})


def _floor_to_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _next_minute_boundary(dt: datetime) -> datetime:
    return _floor_to_minute(dt) + timedelta(minutes=1)


def _is_scout_equity(q: LiveQuote, watchlist: Set[str]) -> bool:
    if q.symbol in _INDEX_SYMBOLS:
        return False
    if q.option_type is not None or q.expiry is not None or q.strike is not None:
        return False
    return q.symbol in watchlist


@dataclass
class _MinuteBar:
    minute: datetime
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    volume_at_start: Optional[int] = None
    tick_count: int = 0

    def to_candle(self) -> Candle:
        return Candle(
            ts=self.minute,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


@dataclass
class _SessionStats:
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0


class ScoutPushEngine:
    """Subscribe to live ticks; fire scout signals on 1m bar close."""

    def __init__(
        self,
        *,
        db: SQLServerConnection,
        spot_lookup: Callable[[str], Optional[float]],
        event_bus: Optional[EventBus] = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._db = db
        self._sig_repo = ScoutSignalRepo(db)
        self._spot_lookup = spot_lookup
        self._bus = event_bus or get_event_bus()
        self._clock = clock
        self._watchlist: Set[str] = set()
        self._reload_watchlist()
        self._dedupe_minutes = int(SCOUT_CONFIG.get("push_dedupe_minutes", 30))

        self._lock = threading.RLock()
        self._history: Dict[str, List[Candle]] = {}
        self._bars: Dict[str, _MinuteBar] = {}
        self._session: Dict[str, _SessionStats] = {}
        self._nifty_open: Optional[float] = None
        self._recent: Dict[Tuple[str, str], datetime] = {}

        self._unsub: Optional[Callable[[], None]] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seeded = False

    def _reload_watchlist(self) -> None:
        from scout.config_loader import get_watchlist

        self._watchlist = {str(s).upper() for s in get_watchlist(self._db)}

    def start(self) -> None:
        if not SCOUT_CONFIG.get("enabled", True):
            logger.info("ScoutPushEngine: disabled in SCOUT_CONFIG")
            return
        if not SCOUT_CONFIG.get("push_enabled", True):
            logger.info("ScoutPushEngine: push_enabled=false — not starting")
            return
        with self._lock:
            if self._unsub is not None:
                return
            self._seed_history()
            self._unsub = self._bus.subscribe(TOPIC_TICK, self._on_tick)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="scout-push-flush",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "ScoutPushEngine: started (%d watchlist symbols)", len(self._watchlist)
        )

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
        logger.info("ScoutPushEngine: stopped")

    def _seed_history(self) -> None:
        if self._seeded or not self._watchlist:
            return
        if not is_market_open():
            self._seeded = True
            return
        try:
            from scout.market_data import ScoutMarketData, ScoutMarketError

            mkt = ScoutMarketData()
            bench_pct = mkt.benchmark_pct_from_open()
            nifty_ltp = self._spot_lookup("NIFTY")
            if nifty_ltp and bench_pct:
                self._nifty_open = float(nifty_ltp) / (1.0 + bench_pct / 100.0)
            for sym in sorted(self._watchlist):
                try:
                    candles, stats = mkt.minute_bars(sym)
                    if candles:
                        self._history[sym] = list(candles)
                    self._session[sym] = _SessionStats(
                        open=float(stats.get("open") or 0),
                        high=float(stats.get("high") or 0),
                        low=float(stats.get("low") or 0),
                    )
                except ScoutMarketError as exc:
                    logger.warning("ScoutPushEngine seed skip %s: %s", sym, exc)
            self._seeded = True
            logger.info(
                "ScoutPushEngine: seeded %d/%d symbols from REST",
                len(self._history),
                len(self._watchlist),
            )
        except Exception:
            logger.exception("ScoutPushEngine: REST seed failed — ticks only")
            self._seeded = True

    def _on_tick(self, q: LiveQuote) -> None:
        if q is None or not self._watchlist:
            return
        if q.symbol == "NIFTY" and self._nifty_open is None and q.last_price:
            self._nifty_open = float(q.last_price)
        if not _is_scout_equity(q, self._watchlist):
            return
        ts = q.timestamp or self._clock()
        minute = _floor_to_minute(ts)
        px = float(q.last_price or 0.0)
        if px <= 0:
            return
        vol = q.volume

        with self._lock:
            sess = self._session.setdefault(sym := q.symbol, _SessionStats())
            if sess.open <= 0:
                sess.open = px
            sess.high = max(sess.high or px, px) if sess.high else px
            sess.low = min(sess.low or px, px) if sess.low else px

            bar = self._bars.get(sym)
            if bar is None or bar.minute != minute:
                if bar is not None and bar.tick_count > 0:
                    self._append_closed_bar(sym, bar)
                bar = _MinuteBar(minute=minute, open=px, high=px, low=px, close=px)
                if vol is not None:
                    bar.volume_at_start = int(vol)
                self._bars[sym] = bar
            else:
                bar.high = max(bar.high, px)
                bar.low = min(bar.low, px)
                bar.close = px
            bar.tick_count += 1
            if vol is not None and bar.volume_at_start is not None:
                bar.volume = float(max(int(vol) - bar.volume_at_start, 0))

    def _append_closed_bar(self, symbol: str, bar: _MinuteBar) -> None:
        candle = bar.to_candle()
        hist = self._history.setdefault(symbol, [])
        if hist and hist[-1].ts == candle.ts:
            hist[-1] = candle
        else:
            hist.append(candle)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            now = self._clock()
            target = _next_minute_boundary(now)
            wait_s = max((target - now).total_seconds(), 0.0)
            if self._stop_event.wait(wait_s):
                return
            if not is_market_open():
                continue
            self._reload_watchlist()
            try:
                self.flush_at(target)
            except Exception:
                logger.exception("ScoutPushEngine: flush failed")

    def flush_at(self, boundary: datetime) -> None:
        """Close bars for the prior minute and evaluate patterns."""
        prior = boundary - timedelta(minutes=1)
        symbols_to_eval: List[str] = []
        with self._lock:
            for sym, bar in list(self._bars.items()):
                if bar.tick_count > 0 and bar.minute == prior:
                    self._append_closed_bar(sym, bar)
                    del self._bars[sym]
            for sym in self._watchlist:
                hist = self._history.get(sym) or []
                if hist and hist[-1].ts == prior:
                    symbols_to_eval.append(sym)

        if not symbols_to_eval:
            return

        bench_pct = self._benchmark_pct()
        scan_id = f"scout-push-{boundary.strftime('%Y%m%d-%H%M')}-{uuid.uuid4().hex[:4]}"
        triggered = boundary
        signals = 0

        for sym in symbols_to_eval:
            with self._lock:
                candles = list(self._history.get(sym) or [])
                sess = self._session.get(sym) or _SessionStats()
            if not min_candles_ok(candles):
                continue
            open_px = sess.open or (candles[0].open if candles else 0.0)
            ltp = candles[-1].close
            day_high = sess.high or max(c.high for c in candles)
            day_low = sess.low or min(c.low for c in candles)
            stock_pct = pct_change(open_px, ltp)
            for sig in detect_signals(
                candles,
                open_px=open_px,
                day_high=day_high,
                day_low=day_low,
                stock_pct=stock_pct,
                bench_pct=bench_pct,
            ):
                key = (sym, sig.signal_type)
                if self._is_duplicate(key, triggered):
                    continue
                row = sig.to_dict()
                self._sig_repo.insert(
                    scan_id=scan_id,
                    symbol=sym,
                    exchange="NSE",
                    action=row["action"],
                    signal_type=row["signal_type"],
                    reason=row["reason"],
                    ltp=float(row["ltp"]),
                    invalidation=row.get("invalidation"),
                    strength=row.get("strength") or "WEAK",
                    triggered_at=triggered,
                    meta={
                        **(row.get("meta") or {}),
                        "source": "ws_push",
                        "stock_pct_from_open": round(stock_pct, 3),
                        "nifty_pct_from_open": round(bench_pct, 3),
                    },
                )
                self._recent[key] = triggered
                signals += 1

        if signals:
            try:
                self._db.commit()
                logger.info(
                    "ScoutPushEngine: %d signal(s) at %s", signals, boundary.isoformat()
                )
            except Exception:
                self._db.rollback()
                logger.exception("ScoutPushEngine: commit failed")

    def _benchmark_pct(self) -> float:
        ltp = self._spot_lookup("NIFTY")
        if ltp is None or self._nifty_open is None or self._nifty_open <= 0:
            return 0.0
        return pct_change(self._nifty_open, float(ltp))

    def _is_duplicate(self, key: Tuple[str, str], at: datetime) -> bool:
        prev = self._recent.get(key)
        if prev is None:
            return False
        return (at - prev).total_seconds() < self._dedupe_minutes * 60
