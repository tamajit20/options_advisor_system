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

    def test_target_hit_fires_when_strategy_take_profit_crossed(self):
        state = _make_state(max_profit=10000.0)
        monitor, notifier, bus = _build_monitor(state, target_fraction=0.70)
        # SELL @ 100 each (qty=50 each). Close at 25 each:
        # current_pnl = 10000 - 2500 = 7500 ≥ IC 50% of max profit → TARGET_HIT.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 25.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 25.0))
        notifier.notify.assert_called_once()
        kwargs = notifier.notify.call_args.kwargs
        assert kwargs["notif_type"] == "TARGET_HIT"
        assert kwargs["severity"] == "INFO"
        assert kwargs["related_trade_id"] == "T-001"

    def test_target_hit_fires_at_eod_take_profit_threshold(self):
        # Live TARGET_HIT uses the same rupee target as Exit Plan / EOD TAKE_PROFIT
        # (IC = 50% of max profit). Previously live required a stricter 70%.
        state = _make_state(max_profit=10000.0)
        monitor, notifier, bus = _build_monitor(state, target_fraction=0.70)
        # Close at 50 each → current_pnl = 5000 = 50% of max profit.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 50.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 50.0))
        notifier.notify.assert_called_once()
        assert notifier.notify.call_args.kwargs["notif_type"] == "TARGET_HIT"

    def test_sl_trigger_fires_when_loss_crosses_threshold(self):
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor(state)
        # Premiums explode: close at 250 each → current_value = -25000
        # current_pnl = 10000 - 25000 = -15000 ≤ -(0.5 * 10000) = -5000 → SL_HIT.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        notifier.notify.assert_called_once()
        kwargs = notifier.notify.call_args.kwargs
        assert kwargs["notif_type"] == "LOSS_LIMIT_HIT"
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
                    "stale_leg_seconds": 9999,
                    "trailing_sl_steps": []},
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
        assert kwargs["severity"] == "INFO"

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
        # May fire LOSS_LIMIT_HIT if loss > 50%, but not a 2nd PRE_BREACH.
        types = [c.kwargs["notif_type"] for c in notifier.notify.call_args_list]
        assert types.count("PRE_BREACH_WARNING") == 1


class TestLossMilestoneHit:
    def test_loss_milestone_fires_at_configured_pct(self, mocker):
        mocker.patch.dict(
            "lifecycle.live_risk_monitor.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_max_loss": 25.0}},
            clear=False,
        )
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor_full(
            state,
            cfg_overrides={"pre_breach_fraction": 0.99, "stale_leg_seconds": 9999},
        )
        monitor._bind_loss_milestone_cfg()
        # SELL @100 → loss at close 130 → pnl -3000 = 30% of max loss (> 25% milestone).
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 130.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 130.0))
        assert notifier.notify.call_count == 1
        kwargs = notifier.notify.call_args.kwargs
        assert kwargs["notif_type"] == "LOSS_MILESTONE_HIT"
        assert kwargs["severity"] == "WARNING"

    def test_hard_sl_still_fires_when_milestone_enabled(self, mocker):
        mocker.patch.dict(
            "lifecycle.live_risk_monitor.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_max_loss": 25.0}},
            clear=False,
        )
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor_full(
            state,
            cfg_overrides={"pre_breach_fraction": 0.99, "stale_leg_seconds": 9999},
        )
        monitor._bind_loss_milestone_cfg()
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        assert notifier.notify.call_count == 1
        assert notifier.notify.call_args.kwargs["notif_type"] == "LOSS_LIMIT_HIT"

    def test_loss_milestone_disabled_skips_alert(self, mocker):
        mocker.patch.dict(
            "lifecycle.live_risk_monitor.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": False, "pct_of_max_loss": 25.0}},
            clear=False,
        )
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor_full(
            state,
            cfg_overrides={"pre_breach_fraction": 0.99, "stale_leg_seconds": 9999},
        )
        monitor._bind_loss_milestone_cfg()
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 130.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 130.0))
        types = [c.kwargs.get("notif_type") for c in notifier.notify.call_args_list]
        assert "LOSS_MILESTONE_HIT" not in types


class TestDTEAwareTarget:
    def test_credit_target_follows_strategy_fraction_at_low_dte(self):
        """IC take-profit is 50% of max profit — same at DTE 1 as at DTE 15."""
        state = _make_state(max_profit=10000.0, max_loss=10000.0)
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
                "target_fraction_at_min_dte": 0.80,
                "target_fraction_at_max_dte": 0.80,
                "target_min_dte": 3, "target_max_dte": 15,
                "stale_leg_seconds": 9999, "pre_breach_fraction": 0.99,
            },
            clock_at=datetime(2026, 5, 5, 11, 0),  # DTE=1
        )
        # 50% pnl (close at 50 each → pnl = 5000) → TARGET_HIT (IC 50%).
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 50.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 50.0))
        assert notifier.notify.call_count == 1
        assert notifier.notify.call_args.kwargs["notif_type"] == "TARGET_HIT"


class TestSpotSL:
    def test_spot_breach_fires_sl_trigger(self):
        """Item #7 — when underlying spot crosses actual_stop_loss_level the
        monitor fires SL_TRIGGER even without leg ticks."""
        expiry = date(2026, 5, 28)
        state = _make_state(max_loss=10000.0)
        state.strategy = "BEAR_CALL_SPREAD"
        state.sl_level = 23950.0  # short 23800 CE + half of 200-wide wing
        state.legs = [
            _LegRef(
                leg_order=1, action="SELL", strike=23800.0, option_type="CE",
                fill_price=100.0, lots=1, lot_size=50,
                key=("NIFTY", expiry, 23800.0, "CE"),
            ),
            _LegRef(
                leg_order=2, action="BUY", strike=24000.0, option_type="CE",
                fill_price=50.0, lots=1, lot_size=50,
                key=("NIFTY", expiry, 24000.0, "CE"),
            ),
        ]
        monitor, notifier, bus = _build_monitor_full(state)
        # Re-index for the new leg.
        monitor._snapshot.index.clear()
        for leg in state.legs:
            monitor._snapshot.index.setdefault(leg.key, []).append(state.trade_id)

        # Spot tick (strike & option_type are None).
        spot_quote = LiveQuote(
            symbol="NIFTY", expiry=None, strike=None, option_type=None,
            last_price=24000.0, source=DataSource.LIVE, provider="zerodha",
        )
        bus.publish("tick", spot_quote)
        assert notifier.notify.call_count == 1
        assert notifier.notify.call_args.kwargs["notif_type"] == "SL_TRIGGER"

    def test_iron_condor_lower_band_breach(self):
        """Iron condor must alert when spot falls through put-side band, not only rally."""
        expiry = date(2026, 5, 28)
        state = _make_state(max_loss=10000.0)
        state.strategy = "IRON_CONDOR"
        state.legs = [
            _LegRef(leg_order=1, action="SELL", strike=23200.0, option_type="PE",
                    fill_price=80.0, lots=1, lot_size=50, key=("NIFTY", expiry, 23200.0, "PE")),
            _LegRef(leg_order=2, action="BUY", strike=22900.0, option_type="PE",
                    fill_price=40.0, lots=1, lot_size=50, key=("NIFTY", expiry, 22900.0, "PE")),
            _LegRef(leg_order=3, action="SELL", strike=24100.0, option_type="CE",
                    fill_price=80.0, lots=1, lot_size=50, key=("NIFTY", expiry, 24100.0, "CE")),
            _LegRef(leg_order=4, action="BUY", strike=24400.0, option_type="CE",
                    fill_price=40.0, lots=1, lot_size=50, key=("NIFTY", expiry, 24400.0, "CE")),
        ]
        state.sl_level = 24250.0
        monitor, notifier, bus = _build_monitor_full(state)
        monitor._snapshot.index.clear()
        for leg in state.legs:
            monitor._snapshot.index.setdefault(leg.key, []).append(state.trade_id)
        bus.publish("tick", LiveQuote(
            symbol="NIFTY", expiry=None, strike=None, option_type=None,
            last_price=23000.0, source=DataSource.LIVE, provider="zerodha",
        ))
        assert notifier.notify.call_count == 1
        assert notifier.notify.call_args.kwargs["notif_type"] == "SL_TRIGGER"

    def test_spot_below_level_does_not_breach_for_short_call(self):
        expiry = date(2026, 5, 28)
        state = _make_state(max_loss=10000.0)
        state.strategy = "BEAR_CALL_SPREAD"
        state.sl_level = 23950.0
        state.legs = [
            _LegRef(
                leg_order=1, action="SELL", strike=23800.0, option_type="CE",
                fill_price=100.0, lots=1, lot_size=50,
                key=("NIFTY", expiry, 23800.0, "CE"),
            ),
            _LegRef(
                leg_order=2, action="BUY", strike=24000.0, option_type="CE",
                fill_price=50.0, lots=1, lot_size=50,
                key=("NIFTY", expiry, 24000.0, "CE"),
            ),
        ]
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

    def test_reload_rebinds_from_strategy_config(self):
        from config import STRATEGY_CONFIG
        from database.config_overlay import restore_file_defaults

        snap = _Snapshot()
        bus = EventBus()
        notifier = MagicMock()
        original = int(STRATEGY_CONFIG["live_risk_monitor"]["cooldown_minutes"])

        def reloader():
            lrm = dict(STRATEGY_CONFIG["live_risk_monitor"])
            lrm["cooldown_minutes"] = 3
            STRATEGY_CONFIG["live_risk_monitor"] = lrm

        monitor = LiveRiskMonitor(
            notifier=notifier,
            snapshot_loader=lambda: snap,
            event_bus=bus,
            config_reloader=reloader,
            clock=lambda: datetime(2026, 5, 5, 11, 0),
        )
        try:
            assert monitor._cooldown.total_seconds() == original * 60
            monitor._reload()
            assert monitor._cooldown.total_seconds() == 3 * 60
        finally:
            restore_file_defaults()


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
        # Floor persists even if TARGET_HIT (same 50% IC threshold) is the
        # alert returned on this tick instead of PROFIT_FLOOR_SET.
        types = [c.kwargs.get("notif_type") for c in notifier.notify.call_args_list]
        assert "TARGET_HIT" in types or "PROFIT_FLOOR_SET" in types

    def test_floor_breach_fires_profit_floor_hit(self):
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
                    if c.kwargs.get("notif_type") == "PROFIT_FLOOR_HIT"]
        assert len(sl_calls) == 1
        assert "profit floor" in sl_calls[0].kwargs.get("body", "").lower()

    def test_loss_limit_after_profit_floor_is_separate_alert(self):
        """Floor breach and later loss limit use distinct notification types."""
        m, notifier, bus, state, _ = self._build_with_trailing(
            steps=[[0.50, 0.0], [0.80, 0.40]])
        # Arm floor at 4000 via 80% profit.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 20.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 20.0))
        assert state.trailing_pnl_floor == 4000.0
        notifier.reset_mock()
        # Profit floor breach (still above loss limit).
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 70.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 70.0))
        floor_calls = [c for c in notifier.notify.call_args_list
                       if c.kwargs.get("notif_type") == "PROFIT_FLOOR_HIT"]
        assert len(floor_calls) == 1
        notifier.reset_mock()
        # Deep loss crosses premium SL — separate alert type.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        loss_calls = [c for c in notifier.notify.call_args_list
                      if c.kwargs.get("notif_type") == "LOSS_LIMIT_HIT"]
        assert len(loss_calls) == 1

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

    def test_mtm_payload_includes_live_outlook(self):
        m, bus, state, captured = self._build()
        state.last_spot = 23000.0
        state.atm_iv = 0.18
        state.entry_pop = 65.0
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 90.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 90.0))
        last = captured[-1]
        assert last["live_pop"] is not None
        assert 0 <= last["live_pop"] <= 100
        assert last["live_ev"] is not None
        assert last["spot"] == 23000.0
        assert last["stance"] in ("improving", "weakening", "stable", "unknown")
        assert "summary" in last

    def test_spot_tick_stores_last_spot_when_sl_disabled(self):
        m, bus, state, captured = self._build()
        m._snapshot.spot_index.setdefault("NIFTY", []).append(state.trade_id)
        m._spot_sl_enabled = False
        bus.publish("tick", LiveQuote(
            symbol="NIFTY", expiry=None, strike=None, option_type=None,
            last_price=23150.0, source=DataSource.LIVE, provider="zerodha",
        ))
        assert state.last_spot == 23150.0

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
            "event_eve_credit_only": True,
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
        # MTM ≈ -2200; SL threshold 50%×10k=5k; event-eve pre_breach 20%→₹1k → fires.
        m, notifier, bus, state, _ = self._build(has_event_tomorrow=True)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 122.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 122.0))
        warns = [c for c in notifier.notify.call_args_list
                 if c.kwargs.get("notif_type") == "PRE_BREACH_WARNING"]
        assert len(warns) == 1, "event-eve tightens pre-breach vs SL threshold"

    def test_no_event_uses_standard_fraction(self):
        # MTM ≈ -1200; standard pre_breach 30%×SL(5k)=₹1.5k → no warning yet.
        m, notifier, bus, state, _ = self._build(has_event_tomorrow=False)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 112.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 112.0))
        warns = [c for c in notifier.notify.call_args_list
                 if c.kwargs.get("notif_type") == "PRE_BREACH_WARNING"]
        assert len(warns) == 0

    def test_event_eve_does_not_tighten_long_vol(self):
        """LONG_STRANGLE keeps standard pre-breach on event eve (credit_only)."""
        m, notifier, bus, state, _ = self._build(has_event_tomorrow=True)
        state.strategy = "LONG_STRANGLE"
        # MTM ≈ -1200; event-eve 20%×SL(5k)=₹1k would fire for credit;
        # long-vol uses 30%×5k=₹1.5k → no warning.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 112.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 112.0))
        warns = [c for c in notifier.notify.call_args_list
                 if c.kwargs.get("notif_type") == "PRE_BREACH_WARNING"]
        assert len(warns) == 0


# ---------------------------------------------------------------------------
# Level breach transition log (ENTER / EXIT)
# ---------------------------------------------------------------------------
class TestLevelEventLog:
    def test_loss_enter_and_exit_on_recovery(self):
        state = _make_state(max_loss=10000.0, max_profit=1_000_000.0)
        clock = {"now": datetime(2026, 5, 5, 11, 0)}
        snap = _Snapshot()
        snap.trades[state.trade_id] = state
        for leg in state.legs:
            snap.index.setdefault(leg.key, []).append(state.trade_id)
        bus = EventBus()
        notifier = MagicMock()
        events = []
        monitor = LiveRiskMonitor(
            notifier=notifier, snapshot_loader=lambda: snap, event_bus=bus,
            config={"enabled": True,
                    "target_fraction_at_min_dte": 0.70,
                    "target_fraction_at_max_dte": 0.70,
                    "cooldown_minutes": 60, "reload_interval_sec": 9999,
                    "session_start": "09:15", "session_end": "15:30",
                    "pre_breach_fraction": 0.99,
                    "stale_leg_seconds": 9999,
                    "trailing_sl_steps": []},
            clock=lambda: clock["now"],
            level_event_persister=lambda p: events.append(dict(p)),
        )
        monitor._snapshot = snap
        monitor._unsubscribe = bus.subscribe("tick", monitor._on_tick)

        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        enters = [e for e in events if e["event_type"] == "ENTER"]
        assert len(enters) == 1
        assert enters[0]["level_type"] == "LOSS_LIMIT"
        assert enters[0]["mtm"] < 0

        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 30.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 30.0))
        exits = [e for e in events if e["event_type"] == "EXIT"]
        assert len(exits) == 1
        assert exits[0]["level_type"] == "LOSS_LIMIT"

        clock["now"] = datetime(2026, 5, 5, 11, 10)
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 280.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 280.0))
        assert sum(1 for e in events if e["event_type"] == "ENTER") == 2


def _make_bput_state():
    """TRD-20260520-001-style bear put spread (debit)."""
    expiry = date(2026, 6, 2)
    legs = [
        _LegRef(
            leg_order=1, action="BUY", strike=23650.0, option_type="PE",
            fill_price=267.0, lots=1, lot_size=75,
            key=("NIFTY", expiry, 23650.0, "PE"),
        ),
        _LegRef(
            leg_order=2, action="SELL", strike=23300.0, option_type="PE",
            fill_price=150.0, lots=1, lot_size=75,
            key=("NIFTY", expiry, 23300.0, "PE"),
        ),
    ]
    return _TradeState(
        trade_id="TRD-BPUT", trade_name="NIFTY-BPUT-JUN1-26",
        strategy="BEAR_PUT_SPREAD", underlying="NIFTY", expiry=expiry,
        entry_net_credit=-8775.0,
        max_profit=17036.25, max_loss=9213.75,
        sl_level=None, legs=legs,
    )


class TestUnifiedProfitAndLegStress:
    def test_bput_short_cheap_does_not_fire_target_when_mtm_negative(self):
        state = _make_bput_state()
        monitor, notifier, bus = _build_monitor(
            state, target_fraction=0.50, clock_at=datetime(2026, 5, 25, 14, 0),
        )
        cfg = monitor._short_leg_stress_enabled
        assert cfg is True
        # Short collapsed but long also down → net loss ~ -5925.
        bus.publish("tick", _q("NIFTY", state.expiry, 23650.0, "PE", 58.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23300.0, "PE", 20.0))
        for call in notifier.notify.call_args_list:
            assert call.kwargs.get("notif_type") != "TARGET_HIT"
        types = [c.kwargs.get("notif_type") for c in notifier.notify.call_args_list]
        assert "PERFECT_CLOSURE" not in types

    def test_short_leg_stress_fires_when_premium_doubles(self):
        state = _make_state()
        monitor, notifier, bus = _build_monitor(
            state, clock_at=datetime(2026, 5, 5, 11, 0),
        )
        # CE at 2× entry; PE still cheap — MTM still above pre-breach zone.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 65.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 200.0))
        types = [c.kwargs.get("notif_type") for c in notifier.notify.call_args_list]
        assert "SHORT_LEG_STRESS" in types
        assert "LOSS_LIMIT_HIT" not in types

    def test_short_leg_stress_suppressed_in_loss_territory(self):
        state = _make_state(max_loss=10000.0)
        monitor, notifier, bus = _build_monitor(
            state,
            clock_at=datetime(2026, 5, 5, 11, 0),
        )
        monitor._pre_breach_fraction = 0.30
        # Both legs explode → deep loss; should get LOSS_LIMIT not leg stress.
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "CE", 250.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23000.0, "PE", 250.0))
        types = [c.kwargs.get("notif_type") for c in notifier.notify.call_args_list]
        assert "LOSS_LIMIT_HIT" in types
        assert "SHORT_LEG_STRESS" not in types

    def test_target_requires_positive_mtm(self):
        state = _make_bput_state()
        monitor, notifier, bus = _build_monitor(
            state, target_fraction=0.01, clock_at=datetime(2026, 5, 25, 14, 0),
        )
        bus.publish("tick", _q("NIFTY", state.expiry, 23650.0, "PE", 58.0))
        bus.publish("tick", _q("NIFTY", state.expiry, 23300.0, "PE", 20.0))
        types = [c.kwargs.get("notif_type") for c in notifier.notify.call_args_list]
        assert "TARGET_HIT" not in types


def test_calendar_spread_mtm_uses_distinct_expiries():
    """Near/far legs at same strike must not share one LTP key."""
    near = date(2026, 8, 28)
    far = date(2026, 9, 30)
    legs = [
        _LegRef(
            leg_order=1, action="SELL", strike=56000.0, option_type="CE",
            fill_price=200.0, lots=1, lot_size=35,
            key=("BANKNIFTY", near, 56000.0, "CE"),
        ),
        _LegRef(
            leg_order=2, action="BUY", strike=56000.0, option_type="CE",
            fill_price=500.0, lots=1, lot_size=35,
            key=("BANKNIFTY", far, 56000.0, "CE"),
        ),
    ]
    state = _TradeState(
        trade_id="TRD-CAL", trade_name="BNIFTY-CALENDAR-AUG4-26",
        strategy="CALENDAR_SPREAD", underlying="BANKNIFTY", expiry=near,
        entry_net_credit=-10500.0,
        max_profit=8000.0, max_loss=10500.0,
        sl_level=None, legs=legs,
    )
    monitor, notifier, bus = _build_monitor(
        state, clock_at=datetime(2026, 8, 17, 15, 20),
    )
    captured = []
    bus.subscribe("trade_mtm", lambda p: captured.append(p))
    bus.publish("tick", _q("BANKNIFTY", near, 56000.0, "CE", 180.0))
    bus.publish("tick", _q("BANKNIFTY", far, 56000.0, "CE", 480.0))
    assert monitor._current_pnl(state) == pytest.approx(0.0, abs=50.0)
    types = [c.kwargs.get("notif_type") for c in notifier.notify.call_args_list]
    assert "LOSS_LIMIT_HIT" not in types
    assert captured
    keys = set(captured[-1]["leg_ltps"])
    assert "BANKNIFTY|2026-08-28|56000.0|CE" in keys
    assert "BANKNIFTY|2026-09-30|56000.0|CE" in keys
    assert captured[-1]["leg_ltps"]["BANKNIFTY|2026-08-28|56000.0|CE"] == pytest.approx(180.0)
    assert captured[-1]["leg_ltps"]["BANKNIFTY|2026-09-30|56000.0|CE"] == pytest.approx(480.0)
