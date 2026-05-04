"""
tests/test_lifecycle/test_opportunity_regen_watcher.py
======================================================

Tests for `lifecycle/opportunity_regen_watcher.py` — intraday tick-driven
hint that the user should re-run the suggestion engine.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

import pytest

from lifecycle.opportunity_regen_watcher import OpportunityRegenWatcher
from providers.base import LiveQuote
from providers.event_bus import EventBus, TOPIC_TICK


_IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _StubNotifier:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def notify(self, notif_type, severity, title, body="", **kw):
        self.events.append({
            "type":     notif_type,
            "severity": severity,
            "title":    title,
            "body":     body,
        })


def _quote(symbol: str, ltp: float) -> LiveQuote:
    """Spot/index tick (option_type=None)."""
    return LiveQuote(
        symbol=symbol, expiry=None, strike=None, option_type=None,
        last_price=ltp,
    )


def _option_quote(ltp: float = 50.0) -> LiveQuote:
    """Option leg tick — must be ignored by the watcher."""
    return LiveQuote(
        symbol="NIFTY", expiry=date(2026, 5, 28), strike=22000.0,
        option_type="CE", last_price=ltp,
    )


def _make_watcher(*, vix=5.0, spot=0.7, day=date(2026, 5, 4)):
    notif = _StubNotifier()
    current = {"day": day}
    clock = lambda: datetime.combine(current["day"], time(10, 0), tzinfo=_IST)
    w = OpportunityRegenWatcher(
        notif,
        vix_threshold_pct=vix,
        spot_threshold_pct=spot,
        clock=clock,
    )
    return w, notif, current


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
class TestConstruction:
    def test_zero_or_negative_threshold_rejected(self):
        with pytest.raises(ValueError):
            OpportunityRegenWatcher(_StubNotifier(), vix_threshold_pct=0)
        with pytest.raises(ValueError):
            OpportunityRegenWatcher(_StubNotifier(), spot_threshold_pct=-1)

    def test_defaults_pulled_from_config(self):
        w = OpportunityRegenWatcher(_StubNotifier())
        # Both thresholds positive (specific values come from STRATEGY_CONFIG)
        assert w._vix_threshold > 0
        assert w._spot_threshold > 0


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------
class TestBaseline:
    def test_first_tick_seeds_baseline_no_alert(self):
        w, notif, _ = _make_watcher()
        w.on_tick(_quote("NIFTY", 22000.0))
        assert notif.events == []

    def test_second_tick_within_threshold_no_alert(self):
        w, notif, _ = _make_watcher(spot=1.0)
        w.on_tick(_quote("NIFTY", 22000.0))         # baseline
        w.on_tick(_quote("NIFTY", 22000.0 * 1.005))  # +0.5%, under 1%
        assert notif.events == []


# ---------------------------------------------------------------------------
# Spot move trigger
# ---------------------------------------------------------------------------
class TestSpotTrigger:
    def test_spot_move_above_threshold_fires_once(self):
        w, notif, _ = _make_watcher(spot=0.7)
        w.on_tick(_quote("NIFTY", 22000.0))             # baseline
        w.on_tick(_quote("NIFTY", 22000.0 * 1.01))      # +1.0%, exceeds 0.7%
        assert len(notif.events) == 1
        ev = notif.events[0]
        assert ev["type"] == "OPPORTUNITY_REGEN_HINT"
        assert ev["severity"] == "INFO"
        assert "NIFTY" in ev["title"]
        assert "+1.00%" in ev["title"] or "+1.0" in ev["title"]

    def test_spot_negative_move_fires(self):
        w, notif, _ = _make_watcher(spot=0.7)
        w.on_tick(_quote("BANKNIFTY", 50000.0))
        w.on_tick(_quote("BANKNIFTY", 50000.0 * 0.99))   # -1.0%
        assert len(notif.events) == 1
        assert "-" in notif.events[0]["title"]

    def test_spot_dedup_per_symbol_per_day(self):
        w, notif, _ = _make_watcher(spot=0.7)
        w.on_tick(_quote("NIFTY", 22000.0))
        w.on_tick(_quote("NIFTY", 22000.0 * 1.01))   # fire
        w.on_tick(_quote("NIFTY", 22000.0 * 1.02))   # already fired — silent
        w.on_tick(_quote("NIFTY", 22000.0 * 1.05))   # still silent
        assert len(notif.events) == 1

    def test_different_symbols_dedup_independently(self):
        w, notif, _ = _make_watcher(spot=0.7)
        w.on_tick(_quote("NIFTY", 22000.0))
        w.on_tick(_quote("BANKNIFTY", 50000.0))
        w.on_tick(_quote("NIFTY", 22000.0 * 1.01))       # fires for NIFTY
        w.on_tick(_quote("BANKNIFTY", 50000.0 * 1.01))   # fires for BANKNIFTY
        assert len(notif.events) == 2
        titles = " | ".join(e["title"] for e in notif.events)
        assert "NIFTY" in titles and "BANKNIFTY" in titles


# ---------------------------------------------------------------------------
# VIX trigger uses its own threshold
# ---------------------------------------------------------------------------
class TestVixTrigger:
    def test_vix_uses_vix_threshold_not_spot_threshold(self):
        # spot threshold low, vix threshold high — make sure VIX uses VIX cfg
        w, notif, _ = _make_watcher(vix=5.0, spot=0.1)
        w.on_tick(_quote("VIX", 14.0))
        w.on_tick(_quote("VIX", 14.0 * 1.03))   # +3% — under VIX 5%
        assert notif.events == []
        w.on_tick(_quote("VIX", 14.0 * 1.06))   # +6% — exceeds VIX 5%
        assert len(notif.events) == 1
        assert "VIX" in notif.events[0]["title"]


# ---------------------------------------------------------------------------
# Day rollover clears state
# ---------------------------------------------------------------------------
class TestDayRollover:
    def test_new_day_resets_baseline_and_dedup(self):
        w, notif, current = _make_watcher(spot=0.7,
                                          day=date(2026, 5, 4))
        w.on_tick(_quote("NIFTY", 22000.0))
        w.on_tick(_quote("NIFTY", 22000.0 * 1.01))   # fires
        assert len(notif.events) == 1

        # Roll to next IST day
        current["day"] = date(2026, 5, 5)
        w.on_tick(_quote("NIFTY", 22500.0))           # new baseline (no fire)
        assert len(notif.events) == 1
        w.on_tick(_quote("NIFTY", 22500.0 * 1.01))   # fires again
        assert len(notif.events) == 2


# ---------------------------------------------------------------------------
# Robustness — bad inputs are silently skipped, never raise
# ---------------------------------------------------------------------------
class TestRobustness:
    def test_option_ticks_ignored(self):
        w, notif, _ = _make_watcher()
        w.on_tick(_option_quote(ltp=50.0))
        w.on_tick(_option_quote(ltp=80.0))   # +60% — but it's an option leg
        assert notif.events == []

    def test_zero_or_negative_ltp_ignored(self):
        w, notif, _ = _make_watcher()
        w.on_tick(_quote("NIFTY", 0.0))
        w.on_tick(_quote("NIFTY", -1.0))
        # No baseline set, no alerts
        assert notif.events == []

    def test_empty_symbol_ignored(self):
        w, notif, _ = _make_watcher()
        w.on_tick(_quote("", 22000.0))
        assert notif.events == []

    def test_notifier_exception_swallowed(self):
        class _Boom:
            def notify(self, *a, **kw):
                raise RuntimeError("downstream blew up")
        w = OpportunityRegenWatcher(
            _Boom(), spot_threshold_pct=0.7,
            clock=lambda: datetime(2026, 5, 4, 10, 0, tzinfo=_IST),
        )
        # baseline + tick that would fire — must NOT raise
        w.on_tick(_quote("NIFTY", 22000.0))
        w.on_tick(_quote("NIFTY", 22000.0 * 1.02))   # exceeds threshold


# ---------------------------------------------------------------------------
# Lifecycle — start/stop subscribes to TOPIC_TICK
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_start_subscribes_and_stop_unsubscribes(self):
        bus = EventBus()
        w = OpportunityRegenWatcher(
            _StubNotifier(),
            event_bus=bus,
            spot_threshold_pct=0.7,
            clock=lambda: datetime(2026, 5, 4, 10, 0, tzinfo=_IST),
        )
        notif = w._notifier
        w.start()
        try:
            bus.publish(TOPIC_TICK, _quote("NIFTY", 22000.0))
            bus.publish(TOPIC_TICK, _quote("NIFTY", 22000.0 * 1.01))
            assert len(notif.events) == 1
        finally:
            w.stop()
        # After stop, further publishes must not reach the watcher.
        bus.publish(TOPIC_TICK, _quote("BANKNIFTY", 50000.0))
        bus.publish(TOPIC_TICK, _quote("BANKNIFTY", 50000.0 * 1.05))
        assert len(notif.events) == 1   # unchanged

    def test_start_is_idempotent(self):
        bus = EventBus()
        w = OpportunityRegenWatcher(
            _StubNotifier(), event_bus=bus,
            clock=lambda: datetime(2026, 5, 4, 10, 0, tzinfo=_IST),
        )
        w.start()
        w.start()   # second call is a no-op, must not raise
        w.stop()

@pytest.mark.future
@pytest.mark.skip(reason="future: PCR-band-cross trigger needs live chain OI (FUTURE_ENHANCEMENT_SCOPES.md -> Risk & Monitoring)")
def test_pcr_band_cross_triggers_regen_hint():
    """When live PCR crosses from the neutral band into strong-bullish (<0.55)
    or strong-bearish (>1.55), the watcher should emit ONE
    OPPORTUNITY_REGEN_HINT per (symbol, day). Requires snapshotting ATM+/-5
    chain OI on each SubscriptionManager reload (or a separate intraday
    chain-OI fetch job)."""
    pass
