"""
tests/test_lifecycle/test_intraday_monitor.py
=============================================

IntradayMonitor — PERFECT_ENTRY only (open-trade alerts live in LiveRiskMonitor).
"""

from __future__ import annotations

from datetime import date, datetime

from lifecycle.intraday_monitor import (
    IntradayMonitor,
    _Snapshot,
    _SuggestionLegRef,
    _to_leg_key,
)
from providers.base import LiveQuote


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


def _build_sug_snap(*legs: _SuggestionLegRef) -> _Snapshot:
    snap = _Snapshot()
    for leg in legs:
        snap.suggestions.setdefault(leg.suggestion_id, []).append(leg)
        snap.suggestion_index.setdefault(leg.key, []).append(leg)
    return snap


def _make_monitor(snap: _Snapshot, *, clock=None) -> tuple[IntradayMonitor, _StubNotifier]:
    notif = _StubNotifier()
    mon = IntradayMonitor(
        notifier=notif,
        snapshot_loader=lambda: snap,
        reload_interval_seconds=3600.0,
        clock=clock or (lambda: datetime(2026, 5, 4, 10, 0, 0)),
    )
    mon._reload_locked()
    return mon, notif


def test_perfect_entry_fires_when_all_legs_in_band():
    k1 = _leg_key(strike=22500.0, opt="CE")
    k2 = _leg_key(strike=21500.0, opt="PE")
    legs = [
        _SuggestionLegRef("SUG-1", "NIFTY-CONDOR", 1, "SELL", 90.0, 85.0, 95.0, k1),
        _SuggestionLegRef("SUG-1", "NIFTY-CONDOR", 2, "SELL", 80.0, 75.0, 85.0, k2),
    ]
    mon, notif = _make_monitor(_build_sug_snap(*legs))
    mon.on_tick(_quote(strike=22500.0, opt="CE", ltp=90.0))
    assert "PERFECT_ENTRY" not in notif.types()
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
    mon.on_tick(_quote(strike=22500.0, opt="CE", ltp=80.0))
    mon.on_tick(_quote(strike=21500.0, opt="PE", ltp=80.0))
    assert "PERFECT_ENTRY" not in notif.types()


def test_perfect_entry_dedups_per_suggestion_per_day():
    leg = _SuggestionLegRef("SUG-1", "x", 1, "SELL", 90.0, 85.0, 95.0, _leg_key())
    mon, notif = _make_monitor(_build_sug_snap(leg))
    mon.on_tick(_quote(ltp=90.0))
    mon.on_tick(_quote(ltp=92.0))
    assert notif.types().count("PERFECT_ENTRY") == 1


def test_spot_ticks_are_ignored():
    leg = _SuggestionLegRef("SUG-1", "x", 1, "SELL", 90.0, 85.0, 95.0, _leg_key())
    mon, notif = _make_monitor(_build_sug_snap(leg))
    mon.on_tick(LiveQuote(symbol="NIFTY", expiry=None, strike=None,
                          option_type=None, last_price=22500.0))
    assert notif.events == []


def test_on_tick_swallows_exceptions(monkeypatch):
    leg = _SuggestionLegRef("SUG-1", "x", 1, "SELL", 90.0, 85.0, 95.0, _leg_key())
    mon, notif = _make_monitor(_build_sug_snap(leg))

    def _boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(mon, "_evaluate_pending_suggestions_locked", _boom)
    mon.on_tick(_quote(ltp=90.0))


def test_to_leg_key_normalises_datetime_expiry_and_lowercase_opt():
    k = _to_leg_key(symbol="NIFTY", expiry=datetime(2026, 5, 28, 15, 30),
                    strike=22000, option_type="ce")
    assert k == ("NIFTY", date(2026, 5, 28), 22000.0, "CE")
