"""Tests for scout push engine minute-bar aggregation."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from providers.base import DataSource, LiveQuote
from scout.candles import Candle
from scout.push_engine import ScoutPushEngine, _SessionStats, _MinuteBar


def test_on_tick_rolls_minute_bar():
    db = MagicMock()
    engine = ScoutPushEngine(db=db, spot_lookup=lambda s: None)
    engine._watchlist = {"RELIANCE"}
    engine._seeded = True

    t1 = datetime(2026, 8, 7, 10, 0, 15)
    t2 = datetime(2026, 8, 7, 10, 1, 5)
    engine._on_tick(_quote("RELIANCE", 100.0, t1, volume=1000))
    engine._on_tick(_quote("RELIANCE", 101.0, t2, volume=1500))

    assert len(engine._history["RELIANCE"]) == 1
    assert engine._history["RELIANCE"][0].close == 100.0
    assert engine._bars["RELIANCE"].minute == datetime(2026, 8, 7, 10, 1, 0)
    assert engine._bars["RELIANCE"].close == 101.0


def test_flush_at_closes_prior_minute_bar():
    db = MagicMock()
    engine = ScoutPushEngine(
        db=db,
        spot_lookup=lambda s: 25000.0 if s == "NIFTY" else None,
    )
    engine._watchlist = {"RELIANCE"}
    engine._seeded = True
    engine._nifty_open = 24900.0
    engine._session["RELIANCE"] = _SessionStats(open=100.0, high=101.0, low=99.5)
    t0 = datetime(2026, 8, 7, 9, 45, 0)
    engine._history["RELIANCE"] = [
        Candle(ts=t0, open=100.0, high=100.5, low=99.8, close=100.2, volume=100)
        for _ in range(12)
    ]
    bar_minute = datetime(2026, 8, 7, 10, 0, 0)
    engine._bars["RELIANCE"] = _MinuteBar(
        minute=bar_minute,
        open=100.5,
        high=101.0,
        low=100.4,
        close=100.9,
        volume=500,
        tick_count=5,
    )

    engine.flush_at(datetime(2026, 8, 7, 10, 1, 0))

    assert engine._history["RELIANCE"][-1].ts == bar_minute
    assert "RELIANCE" not in engine._bars


def _quote(symbol: str, price: float, ts: datetime, volume: int = 1000) -> LiveQuote:
    return LiveQuote(
        symbol=symbol,
        expiry=None,
        strike=None,
        option_type=None,
        last_price=price,
        volume=volume,
        timestamp=ts,
        source=DataSource.LIVE,
        provider="test",
    )
