"""Unit tests for lifecycle.live_risk_monitor."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import List
from unittest.mock import MagicMock

import pytest

from lifecycle.live_risk_monitor import (
    LiveRiskMonitor,
    _LegRef,
    _Snapshot,
    _TradeState,
)
from providers.base import DataSource, LiveQuote
from providers.event_bus import EventBus


def _q(symbol, expiry, strike, ot, last):
    return LiveQuote(
        symbol=symbol, expiry=expiry, strike=strike, option_type=ot,
        last_price=last, source=DataSource.LIVE, provider="zerodha",
    )


def _make_state(*, max_profit=10000.0, max_loss=10000.0, credit=10000.0):
    """Build a 2-leg short-strangle-style trade for testing.

    SELL 23000 CE @ 100 (lots=1, lot_size=50) — fill credit 5000
    SELL 23000 PE @ 100 (lots=1, lot_size=50) — fill credit 5000
    Net credit = 10000.
    """
    expiry = date(2026, 5, 28)
    legs = [
        _LegRef(
            leg_order=1, action="SELL", strike=23000.0, option_type="CE",
            fill_price=100.0, lots=1, lot_size=50,
            key=("NIFTY", expiry, 23000.0, "CE"),
        ),
        _LegRef(
            leg_order=2, action="SELL", strike=23000.0, option_type="PE",
            fill_price=100.0, lots=1, lot_size=50,
            key=("NIFTY", expiry, 23000.0, "PE"),
        ),
    ]
    return _TradeState(
        trade_id="T-001", trade_name="Test Strangle",
        strategy="IRON_CONDOR", underlying="NIFTY", expiry=expiry,
        entry_net_credit=credit, max_profit=max_profit, max_loss=max_loss,
        sl_level=None, legs=legs,
    )


def _build_monitor(state, *, target_fraction=0.70, cooldown_minutes=15,
                    clock_at=datetime(2026, 5, 5, 11, 0)):
    snap = _Snapshot()
    snap.trades[state.trade_id] = state
    for leg in state.legs:
        snap.index.setdefault(leg.key, []).append(state.trade_id)
    notifier = MagicMock()

    bus = EventBus()
    cfg = {
        "enabled": True,
        # Pin the DTE-aware target band to a single value so tests can use
        # `target_fraction` directly regardless of expiry.
        "target_fraction_at_min_dte": target_fraction,
        "target_fraction_at_max_dte": target_fraction,
        "cooldown_minutes": cooldown_minutes,
        "reload_interval_sec": 9999,        # effectively disable background reload
        "session_start": "09:15",
        "session_end": "15:30",
        # Disable the pre-breach soft warning for these specific tests so
        # they assert only the hard SL_TRIGGER / TARGET_HIT path.
        "pre_breach_fraction": 0.99,
        # Allow tick lookups freely — tests publish ticks at the same
        # synthetic clock instant.
        "stale_leg_seconds": 600,
        # Disable trailing SL ratchet for the legacy tests that pre-date #4;
        # dedicated trailing tests below opt in via their own config.
        "trailing_sl_steps": [],
    }
    monitor = LiveRiskMonitor(
        notifier=notifier,
        snapshot_loader=lambda: snap,
        event_bus=bus,
        config=cfg,
        clock=lambda: clock_at,
    )
    monitor._snapshot = snap   # bypass start() so we don't spawn the thread
    monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)
    return monitor, notifier, bus


class TestEvaluation:
    def test_no_alert_when_pnl_in_normal_range(self):
        state = _make_state()
        monitor, notifier, bus = _build_monitor(state)
        # Premiums slightly down → small profit, well below target.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 90.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 90.0))
        notifier.notify.assert_not_called()

    def test_target_hit_fires_when_live_fraction_crossed(self):
        state = _make_state(max_profit=10000.0)
        monitor, notifier, bus = _build_monitor(state, target_fraction=0.70)
        # SELL @ 100 each (qty=50 each). Close at 25 each:
        # current_value = -1 * 25 * 50 (CE close cost) -1 * 25 * 50 (PE close cost) = -2500
        # current_pnl = entry_net_credit (10000) + current_value (-2500) = 7500
        # 7500 >= 0.70 * 10000 → fire TARGET_HIT.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 25.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 25.0))
        notifier.notify.assert_called_once()
        kwargs = notifier.notify.call_args.kwargs
        assert kwargs["notif_type"] == "TARGET_HIT"
        assert kwargs["severity"] == "INFO"
        assert kwargs["related_trade_id"] == "T-001"

    def test_target_not_fired_when_only_eod_threshold_crossed(self):
        # exit_engine considers 0.5 a TAKE_PROFIT, but our live monitor requires 0.7.
        state = _make_state(max_profit=10000.0)
        monitor, notifier, bus = _build_monitor(state, target_fraction=0.70)
        # Close at 50 each → current_value = -5000 → current_pnl = 5000 = 50% of max profit.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 50.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 50.0))
        notifier.notify.assert_not_called()

    def test_sl_trigger_fires_when_loss_crosses_threshold(self):
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor(state)
        # Premiums explode: close at 250 each → current_value = -25000
        # current_pnl = 10000 - 25000 = -15000 ≤ -(0.5 * 10000) = -5000 → SL_HIT.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        notifier.notify.assert_called_once()
        kwargs = notifier.notify.call_args.kwargs
        assert kwargs["notif_type"] == "SL_TRIGGER"
        assert kwargs["severity"] == "CRITICAL"

    def test_does_not_evaluate_until_all_legs_seen(self):
        state = _make_state()
        monitor, notifier, bus = _build_monitor(state)
        # Only one leg ticks → cannot compute MTM yet.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        notifier.notify.assert_not_called()

    def test_session_guard_blocks_alert_outside_market_hours(self):
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor(
            state, clock_at=datetime(2026, 5, 5, 16, 0),  # past 15:30
        )
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        notifier.notify.assert_not_called()


class TestCooldown:
    def test_repeated_breach_within_cooldown_does_not_re_alert(self):
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor(state, cooldown_minutes=15)
        # First breach → fires.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        assert notifier.notify.call_count == 1
        # Second tick at same clock — within cooldown.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 260.0))
        assert notifier.notify.call_count == 1   # still 1

    def test_re_fires_after_cooldown_window(self):
        state = _make_state(max_loss=10000.0)
        # Use a mutable clock holder so we can advance time.
        clock_holder = {"now": datetime(2026, 5, 5, 11, 0)}
        snap = _Snapshot()
        snap.trades[state.trade_id] = state
        for leg in state.legs:
            snap.index.setdefault(leg.key, []).append(state.trade_id)
        bus = EventBus()
        notifier = MagicMock()
        monitor = LiveRiskMonitor(
            notifier=notifier,
            snapshot_loader=lambda: snap,
            event_bus=bus,
            config={"enabled": True,
                    "target_fraction_at_min_dte": 0.70,
                    "target_fraction_at_max_dte": 0.70,
                    "cooldown_minutes": 15, "reload_interval_sec": 9999,
                    "session_start": "09:15", "session_end": "15:30",
                    "pre_breach_fraction": 0.99,
                    "stale_leg_seconds": 9999},
            clock=lambda: clock_holder["now"],
        )
        monitor._snapshot = snap
        monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)

        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        assert notifier.notify.call_count == 1

        # Advance past cooldown.
        clock_holder["now"] = datetime(2026, 5, 5, 11, 16)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        assert notifier.notify.call_count == 2


class TestReloadStopsAlertsOnClosedTrade:
    def test_trade_dropped_on_reload_stops_further_alerts(self):
        state = _make_state(max_loss=10000.0)

        snapshots = [_Snapshot(), _Snapshot()]
        snapshots[0].trades[state.trade_id] = state
        for leg in state.legs:
            snapshots[0].index.setdefault(leg.key, []).append(state.trade_id)
        # Second snapshot is empty (user closed the trade → not ACTIVE anymore).

        loader_calls = {"i": 0}
        def loader():
            i = loader_calls["i"]
            loader_calls["i"] += 1
            return snapshots[min(i, len(snapshots) - 1)]

        bus = EventBus()
        notifier = MagicMock()
        monitor = LiveRiskMonitor(
            notifier=notifier,
            snapshot_loader=loader,
            event_bus=bus,
            config={"enabled": True,
                    "target_fraction_at_min_dte": 0.70,
                    "target_fraction_at_max_dte": 0.70,
                    "cooldown_minutes": 0, "reload_interval_sec": 9999,
                    "session_start": "09:15", "session_end": "15:30",
                    "pre_breach_fraction": 0.99,
                    "stale_leg_seconds": 600},
            clock=lambda: datetime(2026, 5, 5, 11, 0),
        )
        # Simulate first reload (loads state) + subscribe.
        monitor._reload()
        monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)

        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        assert notifier.notify.call_count == 1

        # User closes the trade → next reload returns the empty snapshot.
        monitor._reload()
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 300.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 300.0))
        assert notifier.notify.call_count == 1   # no further alerts


# ---------------------------------------------------------------------------
# Phase 2c-i.1 — improvement-pack tests
# ---------------------------------------------------------------------------

def _build_monitor_full(state, *, cfg_overrides=None,
                         clock_at=datetime(2026, 5, 5, 11, 0)):
    snap = _Snapshot()
    snap.trades[state.trade_id] = state
    for leg in state.legs:
        snap.index.setdefault(leg.key, []).append(state.trade_id)
    snap.spot_index.setdefault(state.underlying, []).append(state.trade_id)

    bus = EventBus()
    notifier = MagicMock()
    cfg = {
        "enabled": True,
        "target_fraction_at_min_dte": 0.70,
        "target_fraction_at_max_dte": 0.70,
        "cooldown_minutes": 15, "reload_interval_sec": 9999,
        "session_start": "09:15", "session_end": "15:30",
        "pre_breach_fraction": 0.30,
        "stale_leg_seconds": 30,
        "spot_sl_enabled": True,
    }
    if cfg_overrides:
        cfg.update(cfg_overrides)
    monitor = LiveRiskMonitor(
        notifier=notifier, snapshot_loader=lambda: snap,
        event_bus=bus, config=cfg, clock=lambda: clock_at,
    )
    monitor._snapshot = snap
    monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)
    return monitor, notifier, bus


class TestStaleGuard:
    def test_evaluation_skipped_when_leg_tick_is_stale(self):
        """Item #1 — if any leg's last tick is older than `stale_leg_seconds`,
        the trade is not evaluated. Avoids alerting on illiquid stale data."""
        state = _make_state(max_loss=10000.0)
        clock = {"now": datetime(2026, 5, 5, 11, 0)}
        snap = _Snapshot()
        snap.trades[state.trade_id] = state
        for leg in state.legs:
            snap.index.setdefault(leg.key, []).append(state.trade_id)
        bus = EventBus()
        notifier = MagicMock()
        monitor = LiveRiskMonitor(
            notifier=notifier, snapshot_loader=lambda: snap, event_bus=bus,
            config={"enabled": True,
                    "target_fraction_at_min_dte": 0.70,
                    "target_fraction_at_max_dte": 0.70,
                    "cooldown_minutes": 15, "reload_interval_sec": 9999,
                    "session_start": "09:15", "session_end": "15:30",
                    "pre_breach_fraction": 0.99,
                    "stale_leg_seconds": 30},
            clock=lambda: clock["now"],
        )
        monitor._snapshot = snap
        monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)

        # CE ticks at t=0; PE ticks at t=0 too.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 100.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 100.0))
        notifier.notify.reset_mock()

        # Advance 60 s — legs are now stale. Only CE re-ticks (would breach).
        clock["now"] = datetime(2026, 5, 5, 11, 1)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        # PE last tick was 60 s ago > 30 s → stale → no alert.
        notifier.notify.assert_not_called()
        assert monitor.stats()["stale_skips"] >= 1


class TestCooldownResetsOnRecovery:
    def test_cooldown_resets_when_breach_clears(self):
        """Item #2 — cooldown should reset once the trade exits breach so the
        next entry into breach alerts immediately."""
        state = _make_state(max_loss=10000.0, max_profit=1_000_000.0)
        clock = {"now": datetime(2026, 5, 5, 11, 0)}
        snap = _Snapshot()
        snap.trades[state.trade_id] = state
        for leg in state.legs:
            snap.index.setdefault(leg.key, []).append(state.trade_id)
        bus = EventBus()
        notifier = MagicMock()
        monitor = LiveRiskMonitor(
            notifier=notifier, snapshot_loader=lambda: snap, event_bus=bus,
            config={"enabled": True,
                    "target_fraction_at_min_dte": 0.70,
                    "target_fraction_at_max_dte": 0.70,
                    "cooldown_minutes": 60, "reload_interval_sec": 9999,
                    "session_start": "09:15", "session_end": "15:30",
                    "pre_breach_fraction": 0.99,
                    "stale_leg_seconds": 9999},
            clock=lambda: clock["now"],
        )
        monitor._snapshot = snap
        monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)

        # Breach 1 — fires.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        assert notifier.notify.call_count == 1

        # Recover — premiums collapse back.
        clock["now"] = datetime(2026, 5, 5, 11, 5)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 30.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 30.0))
        # No new alert (this is recovery, not a new breach).
        assert notifier.notify.call_count == 1

        # Breach again 5 minutes later — cooldown was reset on recovery, so
        # this fires immediately even though < 60 minutes since first breach.
        clock["now"] = datetime(2026, 5, 5, 11, 10)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 280.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 280.0))
        assert notifier.notify.call_count == 2


class TestPreBreachWarning:
    def test_pre_breach_warning_fires_at_30pct_loss(self):
        """Item #5 — soft WARNING when current loss first crosses 30% of max."""
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor_full(
            state, cfg_overrides={"pre_breach_fraction": 0.30,
                                   "stale_leg_seconds": 9999},
        )
        # SELL @ 100 each, qty=50. Close at 130 each → -3000 → pnl = 7000?
        # Wait: current_value = -1*130*50 - 1*130*50 = -13000.
        # pnl = entry_credit (10000) + (-13000) = -3000 → 30% of max loss → fire.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 130.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 130.0))
        assert notifier.notify.call_count == 1
        kwargs = notifier.notify.call_args.kwargs
        assert kwargs["notif_type"] == "PRE_BREACH_WARNING"
        assert kwargs["severity"] == "WARNING"

    def test_pre_breach_fires_only_once_per_day(self):
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor_full(
            state, cfg_overrides={"pre_breach_fraction": 0.30,
                                   "stale_leg_seconds": 9999,
                                   "cooldown_minutes": 0},
        )
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 130.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 130.0))
        assert notifier.notify.call_count == 1
        # Another tick at same loss level — should NOT re-fire pre-breach.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 135.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 135.0))
        # May fire SL_TRIGGER if loss > 50%, but not a 2nd PRE_BREACH.
        types = [c.kwargs["notif_type"] for c in notifier.notify.call_args_list]
        assert types.count("PRE_BREACH_WARNING") == 1


class TestDTEAwareTarget:
    def test_target_tightens_at_low_dte(self):
        """Item #6 — DTE ≤ 3 should require only the lower target fraction."""
        # expiry tomorrow → DTE=1 → uses target_min (0.50).
        state = _make_state(max_profit=10000.0, max_loss=10000.0)
        # Override expiry to be near.
        from dataclasses import replace
        new_legs = [
            _LegRef(leg_order=l.leg_order, action=l.action, strike=l.strike,
                    option_type=l.option_type, fill_price=l.fill_price,
                    lots=l.lots, lot_size=l.lot_size,
                    key=("NIFTY", date(2026, 5, 6), l.strike, l.option_type))
            for l in state.legs
        ]
        state = replace(state, expiry=date(2026, 5, 6), legs=new_legs)

        monitor, notifier, bus = _build_monitor_full(
            state, cfg_overrides={
                "target_fraction_at_min_dte": 0.50,
                "target_fraction_at_max_dte": 0.80,
                "target_min_dte": 3, "target_max_dte": 15,
                "stale_leg_seconds": 9999, "pre_breach_fraction": 0.99,
            },
            clock_at=datetime(2026, 5, 5, 11, 0),  # DTE=1
        )
        # At 50% pnl (close at 50 each → pnl = 5000) → fire because target=0.50.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 50.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 50.0))
        assert notifier.notify.call_count == 1
        assert notifier.notify.call_args.kwargs["notif_type"] == "TARGET_HIT"


class TestSpotSL:
    def test_spot_breach_fires_sl_trigger(self):
        """Item #7 — when underlying spot crosses actual_stop_loss_level the
        monitor fires SL_TRIGGER even without leg ticks."""
        state = _make_state(max_loss=10000.0)
        # Short call dominant → upside SL at 23500. Spot at 23600 → breach.
        state.sl_level = 23500.0
        # Make both legs CE so direction is unambiguous.
        state.legs[1] = _LegRef(
            leg_order=2, action="SELL", strike=23200.0, option_type="CE",
            fill_price=100.0, lots=1, lot_size=50,
            key=("NIFTY", state.expiry, 23200.0, "CE"),
        )
        monitor, notifier, bus = _build_monitor_full(state)
        # Re-index for the new leg.
        monitor._snapshot.index.clear()
        for leg in state.legs:
            monitor._snapshot.index.setdefault(leg.key, []).append(state.trade_id)

        # Spot tick (strike & option_type are None).
        spot_quote = LiveQuote(
            symbol="NIFTY", expiry=None, strike=None, option_type=None,
            last_price=23600.0, source=DataSource.LIVE, provider="zerodha",
        )
        bus.publish("tick", spot_quote)
        assert notifier.notify.call_count == 1
        assert notifier.notify.call_args.kwargs["notif_type"] == "SL_TRIGGER"

    def test_spot_below_level_does_not_breach_for_short_call(self):
        state = _make_state(max_loss=10000.0)
        state.sl_level = 23500.0
        state.legs[1] = _LegRef(
            leg_order=2, action="SELL", strike=23200.0, option_type="CE",
            fill_price=100.0, lots=1, lot_size=50,
            key=("NIFTY", state.expiry, 23200.0, "CE"),
        )
        monitor, notifier, bus = _build_monitor_full(state)
        # Spot well below SL → no alert.
        bus.publish("tick", LiveQuote(
            symbol="NIFTY", expiry=None, strike=None, option_type=None,
            last_price=23000.0, source=DataSource.LIVE, provider="zerodha",
        ))
        notifier.notify.assert_not_called()


class TestSilencedTrade:
    def test_alerts_suppressed_while_silenced(self):
        """Item #11 — `alerts_silenced_until` blocks notifications."""
        state = _make_state(max_loss=10000.0)
        state.silenced_until = datetime(2026, 5, 5, 12, 0)
        monitor, notifier, bus = _build_monitor_full(
            state, cfg_overrides={"stale_leg_seconds": 9999,
                                   "pre_breach_fraction": 0.99},
            clock_at=datetime(2026, 5, 5, 11, 0),
        )
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        notifier.notify.assert_not_called()
        assert monitor.stats()["silenced_skips"] >= 1


class TestMetricsAndReload:
    def test_stats_reports_counters(self):
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor_full(
            state, cfg_overrides={"stale_leg_seconds": 9999,
                                   "pre_breach_fraction": 0.99},
        )
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        s = monitor.stats()
        assert s["ticks_in"] >= 2
        assert s["evaluations"] >= 1
        assert s["alerts_fired"] == 1
        assert s["trades_watched"] == 1

    def test_request_reload_refreshes_snapshot(self):
        state = _make_state(max_loss=10000.0)
        snapshots = [_Snapshot(), _Snapshot()]
        snapshots[0].trades[state.trade_id] = state
        for leg in state.legs:
            snapshots[0].index.setdefault(leg.key, []).append(state.trade_id)
        i = {"n": 0}
        def loader():
            s = snapshots[min(i["n"], 1)]
            i["n"] += 1
            return s

        bus = EventBus()
        notifier = MagicMock()
        monitor = LiveRiskMonitor(
            notifier=notifier, snapshot_loader=loader, event_bus=bus,
            config={"enabled": True,
                    "target_fraction_at_min_dte": 0.70,
                    "target_fraction_at_max_dte": 0.70,
                    "cooldown_minutes": 15, "reload_interval_sec": 9999,
                    "session_start": "09:15", "session_end": "15:30"},
            clock=lambda: datetime(2026, 5, 5, 11, 0),
        )
        monitor._reload()
        assert len(monitor._snapshot.trades) == 1
        monitor.request_reload()
        assert len(monitor._snapshot.trades) == 0


class TestConfigValidation:
    def test_invalid_session_string_falls_back_to_default(self):
        """Item #15 — bad config values must not crash the monitor."""
        state = _make_state()
        snap = _Snapshot()
        snap.trades[state.trade_id] = state
        for leg in state.legs:
            snap.index.setdefault(leg.key, []).append(state.trade_id)
        bus = EventBus()
        notifier = MagicMock()
        # session_start malformed; pre_breach_fraction out of range.
        monitor = LiveRiskMonitor(
            notifier=notifier, snapshot_loader=lambda: snap, event_bus=bus,
            config={"enabled": True, "session_start": "not-a-time",
                    "pre_breach_fraction": 5.0,
                    "target_fraction_at_min_dte": 0.70,
                    "target_fraction_at_max_dte": 0.70,
                    "cooldown_minutes": 15, "reload_interval_sec": 9999},
            clock=lambda: datetime(2026, 5, 5, 11, 0),
        )
        # Falls back to default 09:15 (parseable).
        from datetime import time as dtime
        assert monitor._session_start == dtime(9, 15)


@pytest.mark.future
@pytest.mark.skip(reason="future: per-leg sanity check on tick prices "
                          "(FUTURE_ENHANCEMENT_SCOPES.md → Risk & Monitoring)")
def test_fat_finger_tick_is_rejected():
    """A single tick that jumps >50% above the previous tick should be
    silently rejected (logged as `bad_ticks_skipped`) and must NOT trigger
    SL_TRIGGER. Prevents fat-finger / bad-print false alerts."""
    pass


# ---------------------------------------------------------------------------
# Phase 3 — #4 Trailing SL on profit
# ---------------------------------------------------------------------------
class TestTrailingSL:
    def _build_with_trailing(self, *, steps, clock_at=datetime(2026, 5, 5, 11, 0)):
        state = _make_state()
        snap = _Snapshot()
        snap.trades[state.trade_id] = state
        for leg in state.legs:
            snap.index.setdefault(leg.key, []).append(state.trade_id)
        bus = EventBus()
        notifier = MagicMock()
        persisted = []
        cfg = {
            "enabled": True,
            "target_fraction_at_min_dte": 0.99,
            "target_fraction_at_max_dte": 0.99,
            "cooldown_minutes": 15,
            "reload_interval_sec": 9999,
            "pre_breach_fraction": 0.99,
            "stale_leg_seconds": 600,
            "trailing_sl_steps": steps,
        }
        monitor = LiveRiskMonitor(
            notifier=notifier, snapshot_loader=lambda: snap, event_bus=bus,
            config=cfg, clock=lambda: clock_at,
            trailing_persister=lambda tid, floor, idx: persisted.append(
                (tid, floor, idx)),
        )
        monitor._snapshot = snap
        monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)
        return monitor, notifier, bus, state, persisted

    def test_step_arms_at_50_percent_and_persists(self):
        # Step: at 50% of max profit (₹5000), lock floor at 0% (breakeven).
        m, notifier, bus, state, persisted = self._build_with_trailing(
            steps=[[0.50, 0.0]])
        # Premiums down to 50 each → MTM = 10000 + (-1*50*50) + (-1*50*50) = 5000
        # = 50% of max_profit (10000). Triggers step.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 50.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 50.0))
        assert state.trailing_step_idx == 1
        assert state.trailing_pnl_floor == 0.0
        assert persisted == [("T-001", 0.0, 1)]
        # TARGET_LOCKED notification fired.
        assert any(c.kwargs.get("notif_type") == "TARGET_LOCKED"
                   for c in notifier.notify.call_args_list)

    def test_floor_breach_fires_sl_trigger(self):
        # Two-step: 50% locks breakeven, 80% locks 40% of max.
        m, notifier, bus, state, _ = self._build_with_trailing(
            steps=[[0.50, 0.0], [0.80, 0.40]])
        # Climb to 80% profit → MTM 8000. Premiums down to 20 each.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 20.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 20.0))
        # Floor should now be 0.40 * 10000 = 4000.
        assert state.trailing_pnl_floor == 4000.0
        notifier.reset_mock()
        # MTM falls back to 3000 (premiums 70/70). Below 4000 floor → SL.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 70.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 70.0))
        sl_calls = [c for c in notifier.notify.call_args_list
                    if c.kwargs.get("notif_type") == "SL_TRIGGER"]
        assert len(sl_calls) == 1
        assert "trailing floor" in sl_calls[0].kwargs.get("body", "").lower()

    def test_floor_never_lowers(self):
        # If we cross 80% then drop to 60%, floor must remain 4000 (the 80% lock).
        m, notifier, bus, state, _ = self._build_with_trailing(
            steps=[[0.50, 0.20], [0.80, 0.40]])
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 20.0))  # 80%
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 20.0))
        assert state.trailing_pnl_floor == 4000.0
        # Drop to 60% — must NOT lower floor.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 40.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 40.0))
        assert state.trailing_pnl_floor == 4000.0
        assert state.trailing_step_idx == 2

    def test_invalid_steps_fall_back_to_default(self):
        # Non-ascending triggers — should warn and use defaults from _DEFAULTS.
        from lifecycle.live_risk_monitor import _safe_cfg, _DEFAULTS
        out = _safe_cfg({"trailing_sl_steps": [[0.80, 0.4], [0.50, 0.0]]})
        assert out["trailing_sl_steps"] == [
            tuple(s) for s in _DEFAULTS["trailing_sl_steps"]]


# ---------------------------------------------------------------------------
# Phase 3 — #3 Live MTM streaming
# ---------------------------------------------------------------------------
class TestLiveMTMPublish:
    def _build(self, *, mtm_interval=1.0, clock_at=datetime(2026, 5, 5, 11, 0)):
        state = _make_state()
        snap = _Snapshot()
        snap.trades[state.trade_id] = state
        for leg in state.legs:
            snap.index.setdefault(leg.key, []).append(state.trade_id)
        bus = EventBus()
        notifier = MagicMock()
        cfg = {
            "enabled": True,
            "target_fraction_at_min_dte": 0.99,
            "target_fraction_at_max_dte": 0.99,
            "cooldown_minutes": 15,
            "reload_interval_sec": 9999,
            "pre_breach_fraction": 0.99,
            "stale_leg_seconds": 600,
            "trailing_sl_steps": [],
            "mtm_publish_interval_sec": mtm_interval,
        }
        monitor = LiveRiskMonitor(
            notifier=notifier, snapshot_loader=lambda: snap, event_bus=bus,
            config=cfg, clock=lambda: clock_at,
        )
        monitor._snapshot = snap
        monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)
        captured = []
        bus.subscribe("trade_mtm", lambda p: captured.append(p))
        return monitor, bus, state, captured

    def test_publishes_mtm_payload_on_tick(self):
        m, bus, state, captured = self._build()
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 90.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 90.0))
        assert len(captured) >= 1
        last = captured[-1]
        assert last["trade_id"] == "T-001"
        assert "mtm" in last and "dte" in last and "as_of" in last

    def test_throttle_suppresses_within_window(self):
        # 10s throttle + frozen clock — only 1 publish across many ticks.
        m, bus, state, captured = self._build(mtm_interval=10.0)
        for _ in range(5):
            bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 90.0))
            bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 90.0))
        # 1 publish per leg-tick pair within the throttle window — but the
        # state is shared, so once last_mtm_publish_at is set, subsequent
        # ticks within 10s skip. We expect exactly 1.
        assert len(captured) == 1


# ---------------------------------------------------------------------------
# Phase 3 — #5 Event-eve pre-breach tightening
# ---------------------------------------------------------------------------
class TestEventEvePreBreach:
    def _build(self, *, has_event_tomorrow, clock_at=datetime(2026, 5, 5, 11, 0)):
        state = _make_state(max_loss=10000.0)
        snap = _Snapshot()
        snap.trades[state.trade_id] = state
        for leg in state.legs:
            snap.index.setdefault(leg.key, []).append(state.trade_id)
        bus = EventBus()
        notifier = MagicMock()
        events_repo = MagicMock()
        events_repo.has_high_impact.return_value = has_event_tomorrow
        cfg = {
            "enabled": True,
            "target_fraction_at_min_dte": 0.99,
            "target_fraction_at_max_dte": 0.99,
            "cooldown_minutes": 15,
            "reload_interval_sec": 9999,
            # Standard pre-breach is 30%, event-eve 20%.
            "pre_breach_fraction": 0.30,
            "event_eve_pre_breach_fraction": 0.20,
            "stale_leg_seconds": 600,
            "trailing_sl_steps": [],
        }
        monitor = LiveRiskMonitor(
            notifier=notifier, snapshot_loader=lambda: snap, event_bus=bus,
            config=cfg, clock=lambda: clock_at, events_repo=events_repo,
        )
        monitor._snapshot = snap
        monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)
        return monitor, notifier, bus, state, events_repo

    def test_event_eve_uses_tighter_fraction(self):
        # MTM = -2200 → 22% of max_loss. Below 30% standard but ABOVE 20% eve.
        # Premiums up to 122 each: 10000 - 2*22*50 = 7800. Hmm that's profit.
        # Need MTM ≈ -2200 → premiums up to 122 each (122*50=6100 each, 12200
        # total cost - 10000 credit = -2200 loss). ✓
        m, notifier, bus, state, _ = self._build(has_event_tomorrow=True)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 122.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 122.0))
        warns = [c for c in notifier.notify.call_args_list
                 if c.kwargs.get("notif_type") == "PRE_BREACH_WARNING"]
        assert len(warns) == 1, "event-eve tightens to 20% so 22% loss fires"

    def test_no_event_uses_standard_fraction(self):
        # Same MTM ≈ -2200 → 22% loss. Standard 30% → no warning.
        m, notifier, bus, state, _ = self._build(has_event_tomorrow=False)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 122.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 122.0))
        warns = [c for c in notifier.notify.call_args_list
                 if c.kwargs.get("notif_type") == "PRE_BREACH_WARNING"]
        assert len(warns) == 0



