"""Tests for configurable loss milestone (% of entry premium)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from engine.sl_threshold import (
    apply_profit_milestone_plan,
    build_loss_milestone_plan,
    build_profit_milestone_plan,
    loss_milestone_config,
    loss_milestone_plan_matches_config,
    loss_milestone_rs,
    parse_profit_milestone_plan,
    profit_milestone_config,
    profit_milestone_line_rs,
    profit_milestone_plan_matches_config,
    profit_milestone_rs,
    trade_investment_rs,
)


class TestTradeInvestmentRs:
    def test_debit_is_abs_net_credit(self):
        assert trade_investment_rs(entry_net_credit_rs=-9397.5) == 9397.5

    def test_credit_is_positive_net_credit(self):
        assert trade_investment_rs(entry_net_credit_rs=4500.0) == 4500.0


class TestLossMilestoneThreshold:
    def test_disabled_returns_zero(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": False, "pct_of_premium": 25.0}},
        ):
            rs, pct = loss_milestone_rs(investment_rs=10000.0)
            assert rs == 0.0
            assert pct == 25.0

    def test_enabled_computes_pct_of_premium(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 25.0}},
        ):
            rs, pct = loss_milestone_rs(investment_rs=9397.5)
            assert rs == pytest.approx(2349.375, abs=0.01)
            assert pct == 25.0

    def test_plan_is_fixed_rupees_from_entry_pct(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 25.0}},
        ):
            plan = build_loss_milestone_plan(investment_rs=10000.0)
            assert plan["loss_rs"] == pytest.approx(2500.0)
            assert plan["pct_of_premium"] == 25.0
            assert loss_milestone_plan_matches_config(plan) is True
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 10.0}},
        ):
            assert loss_milestone_plan_matches_config(plan) is False
            rebuilt = build_loss_milestone_plan(investment_rs=10000.0)
            assert rebuilt["loss_rs"] == pytest.approx(1000.0)

    def test_legacy_pct_of_max_loss_fallback(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_max_loss": 20.0}},
        ):
            cfg = loss_milestone_config()
            assert cfg["pct_of_premium"] == 20.0
            rs, pct = loss_milestone_rs(investment_rs=5000.0)
            assert rs == 1000.0
            assert pct == 20.0

    def test_config_clamps_pct(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 150.0}},
        ):
            cfg = loss_milestone_config()
            assert cfg["pct_of_premium"] == 100.0

    def test_auto_close_defaults_true(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert loss_milestone_config()["auto_close"] is True

    def test_auto_close_can_be_disabled(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "auto_close": False,
            }},
        ):
            assert loss_milestone_config()["auto_close"] is False

    def test_confirm_seconds_defaults_to_20(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert loss_milestone_config()["confirm_seconds"] == 20

    def test_confirm_seconds_zero_is_immediate(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"loss_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "confirm_seconds": 0,
            }},
        ):
            assert loss_milestone_config()["confirm_seconds"] == 0


class TestProfitMilestoneThreshold:
    def test_disabled_returns_zero(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": False, "pct_of_premium": 10.0}},
        ):
            rs, pct = profit_milestone_rs(investment_rs=10000.0)
            assert rs == 0.0
            assert pct == 10.0

    def test_enabled_computes_pct_of_premium(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": True, "pct_of_premium": 10.0}},
        ):
            rs, pct = profit_milestone_rs(investment_rs=8000.0)
            assert rs == pytest.approx(800.0)
            assert pct == 10.0

    def test_giveback_floors_at_charges_plus_buffer(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "charges_buffer_rs": 50,
            }},
        ):
            rs, pct = profit_milestone_rs(investment_rs=2000.0, charges_rs=135.0)
            assert rs == pytest.approx(185.0)
            assert pct == 5.0

    def test_charges_buffer_defaults_to_50(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert profit_milestone_config()["charges_buffer_rs"] == pytest.approx(50.0)
            rs, _pct = profit_milestone_rs(investment_rs=2000.0, charges_rs=135.0)
            assert rs == pytest.approx(185.0)

    def test_charges_buffer_can_be_zero(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "charges_buffer_rs": 0,
            }},
        ):
            rs, _pct = profit_milestone_rs(investment_rs=2000.0, charges_rs=135.0)
            assert rs == pytest.approx(135.0)

    def test_giveback_keeps_pct_when_larger_than_charges(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            rs, _pct = profit_milestone_rs(investment_rs=10000.0, charges_rs=135.0)
            assert rs == pytest.approx(500.0)

    def test_line_none_until_peak_covers_giveback(self):
        assert profit_milestone_line_rs(peak_rs=300.0, giveback_rs=400.0) is None
        assert profit_milestone_line_rs(peak_rs=400.0, giveback_rs=400.0) == pytest.approx(0.0)
        assert profit_milestone_line_rs(peak_rs=2000.0, giveback_rs=400.0) == pytest.approx(1600.0)

    def test_sell_line_floored_at_charges(self):
        # peak − giveback = 12, but floor 215 → show/trigger at 215.
        assert profit_milestone_line_rs(
            peak_rs=272.0, giveback_rs=260.0, min_line_rs=215.0,
        ) == pytest.approx(215.0)
        # Trailing above floor wins.
        assert profit_milestone_line_rs(
            peak_rs=2000.0, giveback_rs=260.0, min_line_rs=215.0,
        ) == pytest.approx(1740.0)

    def test_line_none_when_giveback_zero(self):
        assert profit_milestone_line_rs(peak_rs=2000.0, giveback_rs=0.0) is None

    def test_charges_floor_helper(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "charges_buffer_rs": 50,
            }},
        ):
            from engine.sl_threshold import profit_milestone_charges_floor_rs
            assert profit_milestone_charges_floor_rs(charges_rs=135.0) == pytest.approx(185.0)

    def test_max_locks_blank_means_unlimited(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "max_locks": "",
            }},
        ):
            assert profit_milestone_config()["max_locks"] is None
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "max_locks": "  ",
            }},
        ):
            assert profit_milestone_config()["max_locks"] is None
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0,
            }},
        ):
            assert profit_milestone_config()["max_locks"] is None

    def test_max_locks_numeric(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "max_locks": 3,
            }},
        ):
            assert profit_milestone_config()["max_locks"] == 3
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "max_locks": "2",
            }},
        ):
            assert profit_milestone_config()["max_locks"] == 2

    def test_advance_locks_unlimited_tracks_raw_line(self):
        from engine.sl_threshold import advance_profit_milestone_locks
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=1500.0, prev_line_rs=1000.0, lock_count=2, max_locks=None,
        )
        assert line == pytest.approx(1500.0)
        assert count == 2
        assert frozen is False

    def test_advance_locks_freezes_after_n(self):
        from engine.sl_threshold import advance_profit_milestone_locks
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=1000.0, prev_line_rs=None, lock_count=0, max_locks=3,
        )
        assert line == pytest.approx(1000.0) and count == 1 and frozen is False
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=1500.0, prev_line_rs=line, lock_count=count, max_locks=3,
        )
        assert line == pytest.approx(1500.0) and count == 2 and frozen is False
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=2000.0, prev_line_rs=line, lock_count=count, max_locks=3,
        )
        assert line == pytest.approx(2000.0) and count == 3 and frozen is True
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=2500.0, prev_line_rs=line, lock_count=count, max_locks=3,
        )
        assert line == pytest.approx(2000.0) and count == 3 and frozen is True

    def test_advance_locks_giveback_steps_ignore_tiny_ticks(self):
        from engine.sl_threshold import advance_profit_milestone_locks
        line, count, frozen = None, 0, False
        # Charges floor ~218; peak climbs a few rupees at a time.
        floor = 218.0
        giveback = 583.0
        peak = 583.0
        for _ in range(6):
            raw = max(peak - giveback, floor)
            line, count, frozen = advance_profit_milestone_locks(
                raw_line_rs=raw, prev_line_rs=line, lock_count=count,
                max_locks=3, peak_rs=peak, giveback_rs=giveback,
            )
            peak += 2.0
        assert count == 1 and frozen is False
        assert line == pytest.approx(floor)
        # Peak ~1200 still only lock 2 (1200/583 = 2); line trails.
        peak = 1200.0
        raw = max(peak - giveback, floor)
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=raw, prev_line_rs=line, lock_count=count,
            max_locks=3, peak_rs=peak, giveback_rs=giveback,
        )
        assert count == 2 and frozen is False
        assert line == pytest.approx(1200.0 - 583.0)
        # Lock 3 at 3× giveback; freeze at the 3rd-step line (2× giveback),
        # even if peak has already run further.
        peak = 3 * giveback
        raw = max(peak - giveback, floor)
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=raw, prev_line_rs=line, lock_count=count,
            max_locks=3, peak_rs=peak, giveback_rs=giveback,
        )
        assert count == 3 and frozen is True
        assert line == pytest.approx(2 * giveback)
        peak = 2500.0
        raw = max(peak - giveback, floor)
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=raw, prev_line_rs=line, lock_count=count,
            max_locks=3, peak_rs=peak, giveback_rs=giveback,
        )
        assert line == pytest.approx(2 * giveback) and count == 3 and frozen is True

    def test_advance_locks_gap_up_freezes_at_nth_step_not_current_peak(self):
        from engine.sl_threshold import advance_profit_milestone_locks
        giveback = 1138.0
        peak = 15353.0
        raw = peak - giveback
        line, count, frozen = advance_profit_milestone_locks(
            raw_line_rs=raw, prev_line_rs=None, lock_count=0,
            max_locks=3, peak_rs=peak, giveback_rs=giveback,
        )
        assert frozen is True and count == 3
        assert line == pytest.approx(2 * giveback)

    def test_build_plan_three_fixed_levels(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0,
                "charges_buffer_rs": 50, "max_locks": 3,
            }},
        ):
            plan = build_profit_milestone_plan(
                investment_rs=10000.0, charges_rs=150.0,
            )
            assert profit_milestone_plan_matches_config(plan) is True
        assert plan is not None
        assert plan["giveback_rs"] == pytest.approx(500.0)
        assert [lv["peak_rs"] for lv in plan["levels"]] == [500.0, 1000.0, 1500.0]
        # M1 sells at charges floor (200); M2/M3 at 1× / 2× giveback.
        assert [lv["sell_rs"] for lv in plan["levels"]] == [200.0, 500.0, 1000.0]
        assert plan["max_locks"] == 3

    def test_build_plan_n_from_max_locks(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0,
                "charges_buffer_rs": 0, "max_locks": 5,
            }},
        ):
            plan = build_profit_milestone_plan(investment_rs=10000.0, charges_rs=0.0)
        assert [lv["i"] for lv in plan["levels"]] == [1, 2, 3, 4, 5]
        assert plan["levels"][-1]["peak_rs"] == pytest.approx(2500.0)
        assert plan["levels"][-1]["sell_rs"] == pytest.approx(2000.0)

    def test_plan_stale_when_overlay_n_or_pct_changes(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0,
                "charges_buffer_rs": 50, "max_locks": 4,
            }},
        ):
            plan = build_profit_milestone_plan(
                investment_rs=10000.0, charges_rs=150.0,
            )
            assert profit_milestone_plan_matches_config(plan) is True
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0,
                "charges_buffer_rs": 50, "max_locks": 2,
            }},
        ):
            assert profit_milestone_plan_matches_config(plan) is False
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 10.0,
                "charges_buffer_rs": 50, "max_locks": 4,
            }},
        ):
            assert profit_milestone_plan_matches_config(plan) is False
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0,
                "charges_buffer_rs": 50, "max_locks": "",
            }},
        ):
            assert profit_milestone_plan_matches_config(plan) is False
            assert profit_milestone_plan_matches_config(None) is True

    def test_build_plan_none_when_unlimited(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "max_locks": "",
            }},
        ):
            assert build_profit_milestone_plan(investment_rs=10000.0) is None

    def test_apply_plan_pointer_from_peak_never_backwards(self):
        plan = {
            "levels": [
                {"i": 1, "peak_rs": 500, "sell_rs": 200},
                {"i": 2, "peak_rs": 1000, "sell_rs": 500},
                {"i": 3, "peak_rs": 1500, "sell_rs": 1000},
            ],
        }
        assert apply_profit_milestone_plan(plan, peak_rs=100, pointer=0) == (None, 0, False)
        line, ptr, frozen = apply_profit_milestone_plan(plan, peak_rs=500, pointer=0)
        assert line == pytest.approx(200) and ptr == 1 and frozen is False
        # Between M1 and M2 the sell line stays at M1 — no live trail.
        line, ptr, frozen = apply_profit_milestone_plan(plan, peak_rs=800, pointer=1)
        assert line == pytest.approx(200) and ptr == 1 and frozen is False
        line, ptr, frozen = apply_profit_milestone_plan(plan, peak_rs=1500, pointer=1)
        assert line == pytest.approx(1000) and ptr == 3 and frozen is True
        line, ptr, frozen = apply_profit_milestone_plan(plan, peak_rs=100, pointer=3)
        assert line == pytest.approx(1000) and ptr == 3 and frozen is True

    def test_parse_plan_round_trip(self):
        raw = '{"levels":[{"i":1,"peak_rs":500,"sell_rs":50}]}'
        parsed = parse_profit_milestone_plan(raw)
        assert parsed is not None and parsed["levels"][0]["i"] == 1
        assert parse_profit_milestone_plan("not-json") is None
        assert parse_profit_milestone_plan(None) is None

    def test_auto_close_defaults_true(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert profit_milestone_config()["auto_close"] is True

    def test_confirm_seconds_defaults_to_15(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            assert profit_milestone_config()["confirm_seconds"] == 15

    def test_confirm_seconds_override(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_milestone_alert": {
                "enabled": True, "pct_of_premium": 5.0, "confirm_seconds": 20,
            }},
        ):
            assert profit_milestone_config()["confirm_seconds"] == 20

    def test_independent_of_loss_milestone_pct(self):
        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {
                "loss_milestone_alert": {"enabled": True, "pct_of_premium": 5.0},
                "profit_milestone_alert": {"enabled": True, "pct_of_premium": 12.0},
            },
        ):
            assert loss_milestone_config()["pct_of_premium"] == 5.0
            assert profit_milestone_config()["pct_of_premium"] == 12.0


class TestProfitPctAutoClose:
    def test_disabled_by_default_shape(self):
        from engine.sl_threshold import profit_pct_auto_close_config, profit_pct_auto_close_rs

        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_pct_auto_close": {"enabled": False, "pct_of_premium": 5.0}},
        ):
            cfg = profit_pct_auto_close_config()
            assert cfg["enabled"] is False
            assert cfg["pct_of_premium"] == 5.0
            rs, pct = profit_pct_auto_close_rs(investment_rs=10_000.0)
            assert rs == 0.0
            assert pct == 5.0

    def test_enabled_computes_target(self):
        from engine.sl_threshold import profit_pct_auto_close_rs

        with patch(
            "engine.sl_threshold.STRATEGY_CONFIG",
            {"profit_pct_auto_close": {"enabled": True, "pct_of_premium": 5.0}},
        ):
            rs, pct = profit_pct_auto_close_rs(investment_rs=10_000.0)
            assert rs == 500.0
            assert pct == 5.0
