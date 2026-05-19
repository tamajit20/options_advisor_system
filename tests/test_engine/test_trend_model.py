"""Tests for structural + session + return-override trend merge."""
from __future__ import annotations

from datetime import date, timedelta

from engine.trend_model import (
    apply_return_override,
    compute_trends,
    resolve_trend,
    session_trend,
    short_horizon_return_pct,
    short_horizon_trend_from_return,
    upsert_session_bar,
)


def _daily(closes, start: date):
    rows = []
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        rows.append({
            "trade_date": d,
            "open_price": c - 10,
            "high_price": c + 20,
            "low_price": c - 20,
            "close_price": float(c),
        })
    return rows


class TestResolveTrend:
    def test_live_sideways_upgraded_by_session(self):
        assert resolve_trend("SIDEWAYS", "BULLISH", live_mode=True) == "BULLISH"

    def test_conflict_stays_sideways(self):
        assert resolve_trend("BULLISH", "BEARISH", live_mode=True) == "SIDEWAYS"

    def test_eod_ignores_session(self):
        assert resolve_trend("SIDEWAYS", "BULLISH", live_mode=False) == "SIDEWAYS"


class TestReturnOverride:
    def test_sideways_structural_lifted_to_bearish(self):
        assert apply_return_override("SIDEWAYS", "SIDEWAYS", "BEARISH") == "BEARISH"

    def test_structural_bullish_vs_bearish_return_neutralized(self):
        assert apply_return_override("BULLISH", "BULLISH", "BEARISH") == "SIDEWAYS"

    def test_short_horizon_trend_thresholds(self):
        assert short_horizon_trend_from_return(-2.0) == "BEARISH"
        assert short_horizon_trend_from_return(2.0) == "BULLISH"
        assert short_horizon_trend_from_return(-0.5) == "SIDEWAYS"


class TestSessionTrend:
    def test_strong_intraday_move(self):
        hist = _daily([23000] * 10, date(2026, 4, 1))
        bar = {
            "trade_date": date(2026, 4, 10),
            "open_price": 23000.0,
            "high_price": 23200.0,
            "low_price": 22990.0,
            "close_price": 23200.0,
        }
        t = session_trend(
            spot_now=23200.0,
            session_bar=bar,
            spot_history=hist,
            as_of=date(2026, 4, 10),
        )
        assert t == "BULLISH"


class TestComputeTrends:
    def test_live_mode_returns_session(self):
        start = date(2026, 1, 1)
        hist = _daily([22000 + i * 30 for i in range(60)], start)
        as_of = start + timedelta(days=59)
        bar = {
            "trade_date": as_of,
            "open_price": float(hist[-1]["close_price"]),
            "high_price": float(hist[-1]["close_price"]) + 200,
            "low_price": float(hist[-1]["close_price"]) - 50,
            "close_price": float(hist[-1]["close_price"]) + 150,
        }
        eff, struct, sess, ret_pct, ret_tr = compute_trends(
            spot_history=hist,
            as_of=as_of,
            spot_now=float(bar["close_price"]),
            session_bar=bar,
            live_mode=True,
        )
        assert sess in ("BULLISH", "BEARISH", "SIDEWAYS")
        assert eff in ("BULLISH", "BEARISH", "SIDEWAYS")

    def test_eod_return_override_on_recent_drop(self):
        """Flat regime then 5-day selloff → effective BEARISH even if structural chop."""
        start = date(2026, 4, 1)
        closes = [23000.0] * 25 + [22800.0, 22600.0, 22400.0, 22200.0, 22000.0]
        hist = _daily(closes, start)
        as_of = start + timedelta(days=len(closes) - 1)
        ret = short_horizon_return_pct(
            spot_history=hist, as_of=as_of, spot_now=22000.0,
        )
        assert ret is not None and ret <= -1.5
        eff, struct, _, ret_pct, ret_tr = compute_trends(
            spot_history=hist,
            as_of=as_of,
            spot_now=22000.0,
            session_bar=None,
            live_mode=False,
        )
        assert ret_tr == "BEARISH"
        assert eff == "BEARISH"
