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

    def test_conflict_is_mixed_sitout(self):
        assert resolve_trend("BULLISH", "BEARISH", live_mode=True) == "MIXED"
        assert resolve_trend("BEARISH", "BULLISH", live_mode=True) == "MIXED"

    def test_eod_ignores_session(self):
        assert resolve_trend("SIDEWAYS", "BULLISH", live_mode=False) == "SIDEWAYS"


class TestReturnOverride:
    def test_sideways_structural_not_lifted_by_5_10d_tape(self):
        """A week of selling does not become BEARISH while SMA is chop."""
        assert apply_return_override("SIDEWAYS", "SIDEWAYS", "BEARISH") == "SIDEWAYS"
        assert apply_return_override("SIDEWAYS", "SIDEWAYS", "BULLISH") == "SIDEWAYS"

    def test_sideways_not_lifted_even_if_legacy_override_flag_on(self, monkeypatch):
        from config import STRATEGY_CONFIG
        monkeypatch.setitem(STRATEGY_CONFIG, "trend_return_override_structural", True)
        assert apply_return_override("SIDEWAYS", "SIDEWAYS", "BEARISH") == "SIDEWAYS"
        assert apply_return_override("SIDEWAYS", "SIDEWAYS", "BULLISH") == "SIDEWAYS"

    def test_structural_vs_opposing_return_is_mixed(self):
        assert apply_return_override("BULLISH", "BULLISH", "BEARISH") == "MIXED"
        assert apply_return_override("BEARISH", "BEARISH", "BULLISH") == "MIXED"

    def test_agreeing_directional_stays(self):
        assert apply_return_override("BULLISH", "BULLISH", "BULLISH") == "BULLISH"
        assert apply_return_override("BEARISH", "BEARISH", "BEARISH") == "BEARISH"

    def test_weak_return_does_not_undo_sma(self):
        assert apply_return_override("BULLISH", "BULLISH", "SIDEWAYS") == "BULLISH"
        assert apply_return_override("BEARISH", "BEARISH", "SIDEWAYS") == "BEARISH"

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
        assert eff in ("BULLISH", "BEARISH", "SIDEWAYS", "MIXED")

    def test_eod_recent_drop_does_not_become_bearish_from_tape_alone(self):
        """Flat SMA regime then a 5-day selloff is not a bearish thesis.

        Bounce risk is high after a short dump. Sit in SIDEWAYS (range) or MIXED
        (if SMA still bullish) — never lift chop SMA to BEARISH from the return.
        """
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
        if struct == "BEARISH":
            assert eff == "BEARISH"
        elif struct == "BULLISH":
            assert eff == "MIXED"
        else:
            assert struct == "SIDEWAYS"
            assert eff == "SIDEWAYS"

    def test_live_flat_today_after_5d_dump_does_not_become_bearish(self):
        """Chop SMA + last week's dump + flat session today is not BEARISH."""
        start = date(2026, 4, 1)
        closes = [23000.0] * 25 + [22800.0, 22600.0, 22400.0, 22200.0, 22000.0]
        hist = _daily(closes, start)
        as_of = start + timedelta(days=len(closes) - 1)
        bar = {
            "trade_date": as_of,
            "open_price": 22000.0,
            "high_price": 22050.0,
            "low_price": 21950.0,
            "close_price": 22000.0,
        }
        eff, struct, sess, _, ret_tr = compute_trends(
            spot_history=hist,
            as_of=as_of,
            spot_now=22000.0,
            session_bar=bar,
            live_mode=True,
        )
        assert struct == "SIDEWAYS"
        assert sess == "BEARISH"  # 5d session lookback still sees the dump
        assert ret_tr == "BEARISH"
        assert eff == "SIDEWAYS"

    def test_live_same_day_dump_on_chop_sma_is_bearish(self):
        """Intraday open vs spot may lift chop SMA; that is not the 5–10d chase."""
        start = date(2026, 4, 1)
        hist = _daily([23000.0] * 30, start)
        as_of = start + timedelta(days=29)
        bar = {
            "trade_date": as_of,
            "open_price": 23000.0,
            "high_price": 23020.0,
            "low_price": 22880.0,
            "close_price": 22890.0,
        }
        eff, struct, sess, ret_pct, ret_tr = compute_trends(
            spot_history=hist,
            as_of=as_of,
            spot_now=22890.0,
            session_bar=bar,
            live_mode=True,
        )
        assert struct == "SIDEWAYS"
        assert sess == "BEARISH"
        assert ret_tr == "SIDEWAYS"
        assert ret_pct is not None and abs(ret_pct) < 1.5
        assert eff == "BEARISH"
