"""Tests for database/models.py — repo behaviours with mocked SQLServerConnection.

Focus: branching logic, parameter shape, ID generation, idempotency.
We don't assert exact SQL strings (too brittle); we verify that the right
DB methods were called with the right params.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from contracts import (
    FoBhavRow, SpotBhavRow, VixRow, FiiOiRow,
)
from database.models import (
    EventCalendarRepo,
    ExpiryCalendarRepo,
    FiiRepo,
    FoEodRepo,
    IvHistoryRepo,
    LotSizeRepo,
    SpotEodRepo,
    SuggestionRepo,
    TradeRepo,
    VixRepo,
    _safe_float,
)


# ---------------------------------------------------------------------------
# FO EOD
# ---------------------------------------------------------------------------
class TestFoEodRepo:
    def test_upsert_many_empty_returns_zero(self, mock_db):
        n = FoEodRepo(mock_db).upsert_many([])
        assert n == 0
        mock_db.executemany.assert_not_called()

    def test_upsert_many_calls_executemany_with_correct_count(self, mock_db):
        rows = [
            FoBhavRow(date(2026, 4, 30), "NIFTY", "OPTIDX", date(2026, 5, 14),
                      23000, "CE", 1, 2, 0.5, 1.5, 1.5, 100, 50000, 100),
            FoBhavRow(date(2026, 4, 30), "NIFTY", "OPTIDX", date(2026, 5, 14),
                      23000, "PE", 1, 2, 0.5, 1.5, 1.5, 80, 45000, -50),
        ]
        n = FoEodRepo(mock_db).upsert_many(rows)
        assert n == 2
        mock_db.executemany.assert_called_once()
        # Inspect param tuples
        params = mock_db.executemany.call_args[0][1]
        assert len(params) == 2

    def test_get_chain_passes_filters(self, mock_db):
        mock_db.fetch_all.return_value = [{"strike": 23000}]
        FoEodRepo(mock_db).get_chain("NIFTY", date(2026, 4, 30), date(2026, 5, 14))
        sql, params = mock_db.fetch_all.call_args[0]
        assert "options_fo_eod" in sql
        assert params == ["NIFTY", date(2026, 4, 30), date(2026, 5, 14)]

    def test_latest_trade_date_returns_scalar(self, mock_db):
        mock_db.scalar.return_value = date(2026, 4, 30)
        out = FoEodRepo(mock_db).latest_trade_date()
        assert out == date(2026, 4, 30)


# ---------------------------------------------------------------------------
# Spot EOD
# ---------------------------------------------------------------------------
class TestSpotEodRepo:
    def test_for_date_uses_latest_le(self, mock_db):
        """for_date uses 'trade_date <= ? ORDER BY trade_date DESC' to never
        return a future-dated row."""
        SpotEodRepo(mock_db).for_date("NIFTY", date(2026, 4, 30))
        sql, params = mock_db.fetch_one.call_args[0]
        assert "<=" in sql and "ORDER BY trade_date DESC" in sql
        assert params == ["NIFTY", date(2026, 4, 30)]

    def test_history_filters_by_since(self, mock_db):
        SpotEodRepo(mock_db).history("NIFTY", date(2026, 1, 1))
        _, params = mock_db.fetch_all.call_args[0]
        assert params == ["NIFTY", date(2026, 1, 1)]


# ---------------------------------------------------------------------------
# VIX
# ---------------------------------------------------------------------------
class TestVixRepo:
    def test_count_returns_int(self, mock_db):
        mock_db.fetch_one.return_value = {"n": 42}
        assert VixRepo(mock_db).count() == 42

    def test_count_zero_when_no_rows(self, mock_db):
        mock_db.fetch_one.return_value = None
        assert VixRepo(mock_db).count() == 0

    def test_upsert_many_skips_when_empty(self, mock_db):
        VixRepo(mock_db).upsert_many([])
        mock_db.executemany.assert_not_called()


# ---------------------------------------------------------------------------
# FII
# ---------------------------------------------------------------------------
class TestFiiRepo:
    def test_for_date_returns_empty_when_no_data_at_or_before(self, mock_db):
        mock_db.scalar.return_value = None
        out = FiiRepo(mock_db).for_date(date(2026, 4, 30))
        assert out == []
        mock_db.fetch_all.assert_not_called()

    def test_for_date_uses_latest_at_or_before(self, mock_db):
        mock_db.scalar.return_value = date(2026, 4, 28)
        mock_db.fetch_all.return_value = [{"client_type": "FII"}]
        out = FiiRepo(mock_db).for_date(date(2026, 4, 30))
        assert out == [{"client_type": "FII"}]
        # Verify scalar call uses <=
        sql_arg = mock_db.scalar.call_args[0][0]
        assert "<=" in sql_arg


# ---------------------------------------------------------------------------
# Event Calendar
# ---------------------------------------------------------------------------
class TestEventCalendarRepo:
    def test_count_all_zero_when_empty(self, mock_db):
        mock_db.scalar.return_value = 0
        assert EventCalendarRepo(mock_db).count_all() == 0

    def test_count_all_handles_none(self, mock_db):
        mock_db.scalar.return_value = None
        assert EventCalendarRepo(mock_db).count_all() == 0

    def test_has_high_impact_true_when_count_positive(self, mock_db):
        mock_db.scalar.return_value = 1
        assert EventCalendarRepo(mock_db).has_high_impact(
            date(2026, 5, 1), date(2026, 5, 14)
        ) is True

    def test_has_high_impact_false_when_zero(self, mock_db):
        mock_db.scalar.return_value = 0
        assert EventCalendarRepo(mock_db).has_high_impact(
            date(2026, 5, 1), date(2026, 5, 14)
        ) is False

    def test_first_high_impact_event_filters_by_impact(self, mock_db):
        mock_db.fetch_one.return_value = {
            "event_date": date(2026, 5, 7), "event_type": "RBI_MPC", "description": "RBI"
        }
        ev = EventCalendarRepo(mock_db).first_high_impact_event(
            date(2026, 5, 1), date(2026, 5, 14)
        )
        assert ev["event_type"] == "RBI_MPC"
        sql = mock_db.fetch_one.call_args[0][0]
        assert "impact = 'HIGH'" in sql


# ---------------------------------------------------------------------------
# Lot size
# ---------------------------------------------------------------------------
class TestLotSizeRepo:
    def test_for_symbol_returns_lot_size(self, mock_db):
        mock_db.fetch_one.return_value = {"lot_size": 75}
        assert LotSizeRepo(mock_db).for_symbol("NIFTY", date(2026, 5, 1)) == 75

    def test_for_symbol_returns_none_when_missing(self, mock_db):
        mock_db.fetch_one.return_value = None
        assert LotSizeRepo(mock_db).for_symbol("XYZ", date(2026, 5, 1)) is None


# ---------------------------------------------------------------------------
# Suggestion
# ---------------------------------------------------------------------------
class TestSuggestionRepo:
    def test_next_suggestion_id_format(self, mock_db):
        mock_db.fetch_one.return_value = {"m": "SUG-20260430-004"}
        sid = SuggestionRepo(mock_db).next_suggestion_id(date(2026, 4, 30))
        assert sid == "SUG-20260430-005"

    def test_next_suggestion_id_starts_at_001_when_empty(self, mock_db):
        mock_db.fetch_one.return_value = {"m": None}
        sid = SuggestionRepo(mock_db).next_suggestion_id(date(2026, 4, 30))
        assert sid == "SUG-20260430-001"

    def test_next_suggestion_id_handles_null_fetch(self, mock_db):
        mock_db.fetch_one.return_value = None
        sid = SuggestionRepo(mock_db).next_suggestion_id(date(2026, 4, 30))
        assert sid == "SUG-20260430-001"

    def test_next_suggestion_id_skips_deleted_rows(self, mock_db):
        # Regression: even if rows were deleted (count would drop), we must
        # bump from MAX to avoid PK violation.
        mock_db.fetch_one.return_value = {"m": "SUG-20260430-007"}
        sid = SuggestionRepo(mock_db).next_suggestion_id(date(2026, 4, 30))
        assert sid == "SUG-20260430-008"

    def test_has_suggestion_for_with_entry_date_uses_exact_match(self, mock_db):
        mock_db.scalar.return_value = 1
        assert SuggestionRepo(mock_db).has_suggestion_for(
            "NIFTY", date(2026, 4, 30), entry_date=date(2026, 5, 4)
        ) is True
        sql, params = mock_db.scalar.call_args[0]
        assert "entry_date = ?" in sql
        assert date(2026, 5, 4) in params

    def test_has_suggestion_for_returns_false_when_zero(self, mock_db):
        mock_db.scalar.return_value = 0
        assert SuggestionRepo(mock_db).has_suggestion_for(
            "NIFTY", date(2026, 4, 30), entry_date=date(2026, 5, 4)
        ) is False

    def test_has_suggestion_for_legacy_path_uses_generated_on_range(self, mock_db):
        mock_db.scalar.return_value = 0
        SuggestionRepo(mock_db).has_suggestion_for("NIFTY", date(2026, 4, 30))
        sql = mock_db.scalar.call_args[0][0]
        assert "generated_on" in sql

    def test_expire_stale_pending_runs_update(self, mock_db):
        mock_db._cursor.rowcount = 3
        n = SuggestionRepo(mock_db).expire_stale_pending(
            "NIFTY", "WEEKLY", "IRON_CONDOR", date(2026, 5, 4)
        )
        assert n == 3
        sql = mock_db.execute.call_args[0][0]
        assert "UPDATE options_suggestions" in sql
        assert "IGNORED" in sql

    def test_update_status_passes_through(self, mock_db):
        SuggestionRepo(mock_db).update_status("SUG-1", "EXECUTED")
        _, params = mock_db.execute.call_args[0]
        assert params == ["EXECUTED", "SUG-1"]

    def test_write_provenance_skips_when_all_none(self, mock_db):
        SuggestionRepo(mock_db).write_provenance("SUG-1")
        mock_db.execute.assert_not_called()

    def test_write_provenance_only_writes_supplied_fields(self, mock_db):
        SuggestionRepo(mock_db).write_provenance(
            "SUG-1",
            data_source="EOD",
            provider="nse_eod",
            engine_version="v1",
        )
        sql, params = mock_db.execute.call_args[0]
        assert "UPDATE options_suggestions SET" in sql
        # Three fields supplied + suggestion_id at end
        assert params == ["EOD", "nse_eod", "v1", "SUG-1"]
        # Columns not supplied must not appear in the SQL
        assert "trigger_type" not in sql
        assert "live_data_freshness_ms" not in sql

    def test_write_provenance_full_payload(self, mock_db):
        SuggestionRepo(mock_db).write_provenance(
            "SUG-1",
            data_source="LIVE",
            provider="zerodha",
            data_as_of=datetime(2025, 6, 10, 9, 35),
            trigger_type="WS_REGEN",
            trigger_reason="VIX 18.4->19.7",
            market_state_at_gen="OPEN_STABLE",
            live_data_freshness_ms=120,
            engine_version="v1",
        )
        sql, params = mock_db.execute.call_args[0]
        for col in (
            "data_source", "provider", "data_as_of", "trigger_type",
            "trigger_reason", "market_state_at_gen",
            "live_data_freshness_ms", "engine_version",
        ):
            assert col in sql
        assert params[-1] == "SUG-1"
        assert "LIVE" in params and "zerodha" in params and "WS_REGEN" in params


class TestSafeFloat:
    """`_safe_float` shields SQL Server FLOAT columns from inf/NaN values."""

    def test_finite_passes_through(self):
        assert _safe_float(1.5) == 1.5
        assert _safe_float(0) == 0.0
        assert _safe_float(-1234.56) == -1234.56

    def test_int_is_coerced(self):
        assert _safe_float(7) == 7.0

    def test_none_returns_none(self):
        assert _safe_float(None) is None

    def test_inf_becomes_none(self):
        assert _safe_float(float("inf")) is None
        assert _safe_float(float("-inf")) is None

    def test_nan_becomes_none(self):
        assert _safe_float(float("nan")) is None

    def test_non_numeric_returns_none(self):
        assert _safe_float("abc") is None
        assert _safe_float(object()) is None

    def test_numeric_string_is_coerced(self):
        assert _safe_float("3.14") == 3.14


# ---------------------------------------------------------------------------
# Trade
# ---------------------------------------------------------------------------
class TestTradeRepo:
    def test_next_trade_id_format(self, mock_db):
        mock_db.fetch_one.return_value = {"m": None}
        tid = TradeRepo(mock_db).next_trade_id(date(2026, 4, 30))
        assert tid == "TRD-20260430-001"

    def test_void_trade_sets_status(self, mock_db):
        TradeRepo(mock_db).void_trade("TRD-1")
        sql, params = mock_db.execute.call_args[0]
        assert "VOID" in sql
        assert params == ["TRD-1"]

    def test_update_pnl_passes_three_values(self, mock_db):
        TradeRepo(mock_db).update_pnl("TRD-1", 1000.0, 50.0, 950.0)
        _, params = mock_db.execute.call_args[0]
        assert params == [1000.0, 50.0, 950.0, "TRD-1"]

    def test_write_execution_provenance_skips_when_all_none(self, mock_db):
        TradeRepo(mock_db).write_execution_provenance("TRD-1")
        mock_db.execute.assert_not_called()

    def test_write_execution_provenance_partial(self, mock_db):
        TradeRepo(mock_db).write_execution_provenance(
            "TRD-1",
            execution_data_source="EOD",
            gate_passed=True,
            time_from_suggestion_sec=42,
        )
        sql, params = mock_db.execute.call_args[0]
        assert "UPDATE options_trades SET" in sql
        assert "execution_data_source" in sql
        assert "gate_passed" in sql
        assert "time_from_suggestion_sec" in sql
        assert "execution_provider" not in sql
        assert params == ["EOD", True, 42, "TRD-1"]


# ---------------------------------------------------------------------------
# Expiry calendar — recompute logic
# ---------------------------------------------------------------------------
class TestExpiryCalendarRepo:
    def test_no_strategy_underlyings_returns_zero(self, mock_db):
        rows = [
            FoBhavRow(date(2026, 4, 30), "RANDOMSYM", "OPTSTK", date(2026, 5, 14),
                      100, "CE", 1, 1, 1, 1, 1, 0, 0, 0),
        ]
        n = ExpiryCalendarRepo(mock_db).upsert_from_fo_rows(rows)
        assert n == 0
        mock_db.executemany.assert_not_called()

    def test_filters_to_strategy_underlyings(self, mock_db):
        from config import STRATEGY_CONFIG
        target = STRATEGY_CONFIG["underlyings"][0]
        rows = [
            FoBhavRow(date(2026, 4, 30), target, "OPTIDX", date(2026, 5, 14),
                      23000, "CE", 1, 1, 1, 1, 1, 0, 0, 0),
            FoBhavRow(date(2026, 4, 30), "NOTAREALSYMBOL", "OPTSTK",
                      date(2026, 5, 14), 100, "CE", 1, 1, 1, 1, 1, 0, 0, 0),
        ]
        n = ExpiryCalendarRepo(mock_db).upsert_from_fo_rows(rows)
        assert n == 1  # only the strategy underlying counted
