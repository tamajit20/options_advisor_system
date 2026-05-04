"""
tests/test_lifecycle/test_intraday_monitor.py
=============================================

Phase 2b-iii — IntradayMonitor unit tests.

We never use the real DB or event bus; a hand-rolled snapshot is fed to the
monitor, ticks are pushed via `monitor.on_tick(...)` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional

import pytest

from lifecycle.intraday_monitor import (
    IntradayMonitor,
    _Snapshot,
    _SuggestionLegRef,
    _TradeLegRef,
    _to_leg_key,
)
from providers.base import LiveQuote


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _StubNotifier:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def notify(self, notif_type, severity, title, body="", *,
               related_suggestion_id=None, related_trade_id=None,
               bypass_flags=False):
        self.events.append({
            "type": notif_type, "severity": severity, "title": title,
            "body": body, "sid": related_suggestion_id, "tid": related_trade_id,
        })

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


def _quote(symbol="NIFTY", expiry=date(2026, 5, 28), strike=22000.0,
           opt="CE", ltp=100.0) -> LiveQuote:
    return LiveQuote(
        symbol=symbol, expiry=expiry, strike=strike, option_type=opt,
        last_price=ltp,
    )


def _leg_key(symbol="NIFTY", expiry=date(2026, 5, 28), strike=22000.0, opt="CE"):
    return (symbol, expiry, strike, opt)


def _build_trade_snap(*legs: _TradeLegRef) -> _Snapshot:
    snap = _Snapshot()
    for leg in legs:
        snap.trades.setdefault(leg.trade_id, []).append(leg)
        snap.trade_index.setdefault(leg.key, []).append(leg)
    return snap


def _build_sug_snap(*legs: _SuggestionLegRef) -> _Snapshot:
    snap = _Snapshot()
    for leg in legs:
        snap.suggestions.setdefault(leg.suggestion_id, []).append(leg)
        snap.suggestion_index.setdefault(leg.key, []).append(leg)
    return snap


def _make_monitor(snap: _Snapshot, *, sl_multiplier=2.0,
                  clock=None) -> tuple[IntradayMonitor, _StubNotifier]:
    notif = _StubNotifier()
    loader_calls = {"n": 0}

    def _loader():
        loader_calls["n"] += 1
        return snap

    mon = IntradayMonitor(
        notifier=notif,
        snapshot_loader=_loader,
        sl_multiplier=sl_multiplier,
        reload_interval_seconds=3600.0,  # huge — disables auto-reload during the test
        clock=clock or (lambda: datetime(2026, 5, 4, 10, 0, 0)),
    )
    mon._reload_locked()  # prime
    return mon, notif


# ---------------------------------------------------------------------------
# SL_TRIGGER
# ---------------------------------------------------------------------------
def test_sl_trigger_fires_when_short_premium_doubles():
    leg = _TradeLegRef(
        trade_id="TRD-1", trade_name="NIFTY-CONDOR", strategy="IRON_CONDOR",
        leg_order=1, action="SELL", fill_price=50.0, key=_leg_key(),
    )
    mon, notif = _make_monitor(_build_trade_snap(leg))
    mon.on_tick(_quote(ltp=100.0))
    assert notif.types() == ["SL_TRIGGER"]
    assert notif.events[0]["severity"] == "CRITICAL"
    assert notif.events[0]["tid"] == "TRD-1"


def test_sl_trigger_does_not_fire_below_threshold():
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg))
    mon.on_tick(_quote(ltp=99.0))
    assert notif.events == []


def test_sl_trigger_ignores_long_legs():
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "BUY", 50.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg))
    mon.on_tick(_quote(ltp=500.0))  # huge spike but it's a LONG leg → no SL
    assert notif.events == []


def test_sl_trigger_dedups_per_leg_per_day():
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg))
    mon.on_tick(_quote(ltp=120.0))
    mon.on_tick(_quote(ltp=130.0))
    mon.on_tick(_quote(ltp=200.0))
    assert notif.types() == ["SL_TRIGGER"]


def test_sl_trigger_dedup_resets_on_new_ist_day():
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    notif = _StubNotifier()
    current = {"t": datetime(2026, 5, 4, 10, 0, 0)}
    clock = lambda: current["t"]
    mon = IntradayMonitor(
        notifier=notif, snapshot_loader=lambda: _build_trade_snap(leg),
        sl_multiplier=2.0, reload_interval_seconds=3600.0, clock=clock,
    )
    mon._reload_locked()
    current["t"] = datetime(2026, 5, 4, 10, 0, 1)
    mon.on_tick(_quote(ltp=120.0))
    current["t"] = datetime(2026, 5, 5, 10, 0, 0)  # next IST day
    mon.on_tick(_quote(ltp=120.0))
    assert notif.types() == ["SL_TRIGGER", "SL_TRIGGER"]


def test_sl_uses_custom_multiplier():
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg), sl_multiplier=3.0)
    mon.on_tick(_quote(ltp=120.0))   # 50 * 3 = 150 → no fire
    assert notif.events == []
    mon.on_tick(_quote(ltp=160.0))
    assert notif.types() == ["SL_TRIGGER"]


# ---------------------------------------------------------------------------
# PERFECT_CLOSURE
# ---------------------------------------------------------------------------
def test_perfect_closure_fires_when_all_shorts_at_capture_target():
    # IRON_CONDOR → take_profit_fraction = 0.50 → close when ltp ≤ 50% of fill
    k1 = _leg_key(strike=22500.0, opt="CE")
    k2 = _leg_key(strike=21500.0, opt="PE")
    legs = [
        _TradeLegRef("TRD-1", "NIFTY-CONDOR", "IRON_CONDOR", 1, "SELL", 100.0, k1),
        _TradeLegRef("TRD-1", "NIFTY-CONDOR", "IRON_CONDOR", 2, "SELL", 80.0,  k2),
    ]
    mon, notif = _make_monitor(_build_trade_snap(*legs))
    # First leg hits target — but the other hasn't ticked → no closure yet
    mon.on_tick(_quote(strike=22500.0, opt="CE", ltp=40.0))
    assert "PERFECT_CLOSURE" not in notif.types()
    # Second leg hits target → closure fires
    mon.on_tick(_quote(strike=21500.0, opt="PE", ltp=30.0))
    assert "PERFECT_CLOSURE" in notif.types()
    closure = [e for e in notif.events if e["type"] == "PERFECT_CLOSURE"][0]
    assert closure["tid"] == "TRD-1"
    assert closure["severity"] == "INFO"


def test_perfect_closure_requires_strict_at_or_below():
    legs = [
        _TradeLegRef("TRD-1", "x", "IRON_CONDOR", 1, "SELL", 100.0, _leg_key(strike=22500.0, opt="CE")),
        _TradeLegRef("TRD-1", "x", "IRON_CONDOR", 2, "SELL", 80.0,  _leg_key(strike=21500.0, opt="PE")),
    ]
    mon, notif = _make_monitor(_build_trade_snap(*legs))
    mon.on_tick(_quote(strike=22500.0, opt="CE", ltp=51.0))  # > 50 → not at target
    mon.on_tick(_quote(strike=21500.0, opt="PE", ltp=30.0))
    assert "PERFECT_CLOSURE" not in notif.types()


def test_perfect_closure_dedups_per_trade_per_day():
    leg = _TradeLegRef("TRD-1", "x", "IRON_CONDOR", 1, "SELL", 100.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg))
    mon.on_tick(_quote(ltp=40.0))
    mon.on_tick(_quote(ltp=30.0))
    mon.on_tick(_quote(ltp=20.0))
    assert notif.types().count("PERFECT_CLOSURE") == 1


def test_perfect_closure_ignores_long_legs_in_target_check():
    # A naked-call style trade: 1 SELL + 1 BUY hedge. Closure depends ONLY on the SELL.
    legs = [
        _TradeLegRef("TRD-1", "x", "BULL_PUT_SPREAD", 1, "SELL", 100.0, _leg_key(strike=22000.0, opt="PE")),
        _TradeLegRef("TRD-1", "x", "BULL_PUT_SPREAD", 2, "BUY",  20.0,  _leg_key(strike=21500.0, opt="PE")),
    ]
    mon, notif = _make_monitor(_build_trade_snap(*legs))
    # Tick only the SELL — its 50% target is met. The BUY leg's price is irrelevant.
    mon.on_tick(_quote(strike=22000.0, opt="PE", ltp=40.0))
    assert "PERFECT_CLOSURE" in notif.types()


def test_perfect_closure_uses_default_fraction_for_unknown_strategy():
    # Unknown strategy → falls back to take_profit_fraction (0.80) → ltp must ≤ 20% of fill
    leg = _TradeLegRef("TRD-1", "x", "WEIRD_NEW_STRAT", 1, "SELL", 100.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg))
    mon.on_tick(_quote(ltp=25.0))   # 25 > 20 → no closure
    assert "PERFECT_CLOSURE" not in notif.types()
    mon.on_tick(_quote(ltp=15.0))
    assert "PERFECT_CLOSURE" in notif.types()


# ---------------------------------------------------------------------------
# PERFECT_ENTRY
# ---------------------------------------------------------------------------
def test_perfect_entry_fires_when_all_legs_in_band():
    k1 = _leg_key(strike=22500.0, opt="CE")
    k2 = _leg_key(strike=21500.0, opt="PE")
    legs = [
        _SuggestionLegRef("SUG-1", "NIFTY-CONDOR", 1, "SELL", 90.0, 85.0, 95.0, k1),
        _SuggestionLegRef("SUG-1", "NIFTY-CONDOR", 2, "SELL", 80.0, 75.0, 85.0, k2),
    ]
    mon, notif = _make_monitor(_build_sug_snap(*legs))
    mon.on_tick(_quote(strike=22500.0, opt="CE", ltp=90.0))
    assert "PERFECT_ENTRY" not in notif.types()  # only one leg seen so far
    mon.on_tick(_quote(strike=21500.0, opt="PE", ltp=80.0))
    assert "PERFECT_ENTRY" in notif.types()
    ev = [e for e in notif.events if e["type"] == "PERFECT_ENTRY"][0]
    assert ev["sid"] == "SUG-1"


def test_perfect_entry_does_not_fire_when_leg_outside_band():
    k1 = _leg_key(strike=22500.0, opt="CE")
    k2 = _leg_key(strike=21500.0, opt="PE")
    legs = [
        _SuggestionLegRef("SUG-1", "x", 1, "SELL", 90.0, 85.0, 95.0, k1),
        _SuggestionLegRef("SUG-1", "x", 2, "SELL", 80.0, 75.0, 85.0, k2),
    ]
    mon, notif = _make_monitor(_build_sug_snap(*legs))
    mon.on_tick(_quote(strike=22500.0, opt="CE", ltp=80.0))   # below low (85)
    mon.on_tick(_quote(strike=21500.0, opt="PE", ltp=80.0))
    assert "PERFECT_ENTRY" not in notif.types()


def test_perfect_entry_dedups_per_suggestion_per_day():
    leg = _SuggestionLegRef("SUG-1", "x", 1, "SELL", 90.0, 85.0, 95.0, _leg_key())
    mon, notif = _make_monitor(_build_sug_snap(leg))
    mon.on_tick(_quote(ltp=90.0))
    mon.on_tick(_quote(ltp=92.0))
    assert notif.types().count("PERFECT_ENTRY") == 1


# ---------------------------------------------------------------------------
# Tick filtering / robustness
# ---------------------------------------------------------------------------
def test_spot_index_ticks_are_ignored():
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg))
    spot_quote = LiveQuote(symbol="NIFTY", expiry=None, strike=None,
                           option_type=None, last_price=22500.0)
    mon.on_tick(spot_quote)
    assert notif.events == []


def test_zero_or_missing_ltp_is_ignored():
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg))
    mon.on_tick(_quote(ltp=0.0))
    assert notif.events == []


def test_on_tick_swallows_exceptions(monkeypatch):
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    mon, notif = _make_monitor(_build_trade_snap(leg))

    # Force an error inside the evaluator. on_tick MUST NOT raise.
    def _boom(*a, **kw):
        raise RuntimeError("x")
    monkeypatch.setattr(mon, "_evaluate_active_trades_locked", _boom)
    mon.on_tick(_quote(ltp=120.0))   # would have fired SL_TRIGGER
    # Test passes if no exception escapes.
    assert True


def test_notifier_exception_does_not_break_monitor():
    class _BadNotifier:
        def notify(self, *a, **kw):
            raise RuntimeError("downstream broken")

    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    mon = IntradayMonitor(
        notifier=_BadNotifier(),
        snapshot_loader=lambda: _build_trade_snap(leg),
        sl_multiplier=2.0,
        reload_interval_seconds=3600.0,
        clock=lambda: datetime(2026, 5, 4, 10, 0, 0),
    )
    mon._reload_locked()
    mon.on_tick(_quote(ltp=120.0))   # should not raise


# ---------------------------------------------------------------------------
# Reload cadence
# ---------------------------------------------------------------------------
def test_snapshot_reloads_after_interval():
    snaps = [_Snapshot(), _Snapshot()]
    leg = _TradeLegRef("TRD-1", "x", "IC", 1, "SELL", 50.0, _leg_key())
    snaps[1].trades["TRD-1"] = [leg]
    snaps[1].trade_index[leg.key] = [leg]

    current = {"t": datetime(2026, 5, 4, 10, 0, 0)}
    clock = lambda: current["t"]
    calls = {"n": 0}

    def _loader():
        calls["n"] += 1
        return snaps.pop(0) if snaps else _Snapshot()

    mon = IntradayMonitor(
        notifier=_StubNotifier(), snapshot_loader=_loader,
        reload_interval_seconds=60.0, clock=clock,
    )
    mon.start()                       # primes (call 1)
    current["t"] = datetime(2026, 5, 4, 10, 0, 1)
    mon.on_tick(_quote(ltp=120.0))    # within 60s → no reload (still call 1)
    assert calls["n"] == 1
    current["t"] = datetime(2026, 5, 4, 10, 1, 30)
    mon.on_tick(_quote(ltp=120.0))    # 90s elapsed → reload (call 2)
    assert calls["n"] == 2
    mon.stop()


# ---------------------------------------------------------------------------
# _to_leg_key normalisation
# ---------------------------------------------------------------------------
def test_to_leg_key_normalises_datetime_expiry_and_lowercase_opt():
    k = _to_leg_key(symbol="NIFTY", expiry=datetime(2026, 5, 28, 15, 30),
                    strike=22000, option_type="ce")
    assert k == ("NIFTY", date(2026, 5, 28), 22000.0, "CE")


def test_to_leg_key_handles_none_expiry_for_spot():
    k = _to_leg_key(symbol="NIFTY", expiry=None, strike=None, option_type=None)
    assert k == ("NIFTY", None, None, None)
