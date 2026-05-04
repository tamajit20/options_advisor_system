"""
tests/test_engine/test_circuit_breaker.py
=========================================

Pure-function tests for engine/circuit_breaker.py.
"""

from __future__ import annotations

import pytest

from engine.circuit_breaker import check_daily_pnl_breach


class TestNoBreach:
    def test_winning_returns_none(self):
        assert check_daily_pnl_breach(
            total_pnl_rs=5_000.0, capital_rs=500_000.0, limit_pct=3.0
        ) is None

    def test_flat_returns_none(self):
        assert check_daily_pnl_breach(
            total_pnl_rs=0.0, capital_rs=500_000.0, limit_pct=3.0
        ) is None

    def test_within_limit_returns_none(self):
        # Limit at 3% of 500k = 15k. -10k is within budget.
        assert check_daily_pnl_breach(
            total_pnl_rs=-10_000.0, capital_rs=500_000.0, limit_pct=3.0
        ) is None

    def test_at_limit_exactly_returns_none(self):
        # Strictly less than -limit triggers; equal does not.
        assert check_daily_pnl_breach(
            total_pnl_rs=-15_000.0, capital_rs=500_000.0, limit_pct=3.0
        ) is None


class TestBreach:
    def test_just_past_limit_breaches(self):
        result = check_daily_pnl_breach(
            total_pnl_rs=-15_001.0, capital_rs=500_000.0, limit_pct=3.0,
        )
        assert result is not None
        assert result.breached is True
        assert result.limit_rs == 15_000.0
        assert "breach" in result.headline.lower()

    def test_pct_of_capital_negative_when_losing(self):
        result = check_daily_pnl_breach(
            total_pnl_rs=-30_000.0, capital_rs=500_000.0, limit_pct=3.0,
        )
        assert result is not None
        assert result.pct_of_capital == -6.0


class TestMisconfig:
    def test_zero_capital_returns_none(self):
        assert check_daily_pnl_breach(
            total_pnl_rs=-99_999.0, capital_rs=0.0, limit_pct=3.0
        ) is None

    def test_zero_limit_returns_none(self):
        assert check_daily_pnl_breach(
            total_pnl_rs=-99_999.0, capital_rs=500_000.0, limit_pct=0.0
        ) is None


class TestConfigDefaults:
    def test_uses_strategy_config_when_args_omitted(self, mocker):
        from engine import circuit_breaker as cb
        mocker.patch.dict(cb.STRATEGY_CONFIG, {
            "daily_pnl_circuit_breaker_capital_rs": 100_000.0,
            "daily_pnl_circuit_breaker_pct": 5.0,
        })
        # 5% of 100k = 5k. -5001 breaches.
        result = cb.check_daily_pnl_breach(total_pnl_rs=-5_001.0)
        assert result is not None
        assert result.limit_rs == 5_000.0


# ---------------------------------------------------------------------------
# Validator integration: cb_active=True is a hard veto
# ---------------------------------------------------------------------------
class TestValidatorIntegration:
    def test_cb_active_blocks_otherwise_clean_suggestion(self):
        from engine.execution_validator import validate_execution
        from datetime import date, datetime
        sug = {
            "status": "PENDING", "validator_status": None,
            "entry_date": date(2026, 5, 4),
            "data_as_of": datetime(2026, 5, 4, 9, 16),
            "spot_at_generation": 23000.0,
        }
        legs = [
            {"leg_order": 1, "strike": 23500.0, "option_type": "CE",
             "action": "SELL"},
            {"leg_order": 2, "strike": 22500.0, "option_type": "PE",
             "action": "SELL"},
        ]
        # Without flag: passes
        ok = validate_execution(
            sug, legs,
            now=datetime(2026, 5, 4, 9, 16), today=date(2026, 5, 4),
            circuit_breaker_active=False,
        )
        assert ok.ok
        # With flag: hard veto
        bad = validate_execution(
            sug, legs,
            now=datetime(2026, 5, 4, 9, 16), today=date(2026, 5, 4),
            circuit_breaker_active=True,
        )
        assert not bad.ok
        assert any("circuit breaker" in v for v in bad.vetoes)
