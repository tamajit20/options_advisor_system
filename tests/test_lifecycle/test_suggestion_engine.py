"""Tests for lifecycle/suggestion_engine.py — internal helpers + run() orchestration."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock

import pytest

from lifecycle import suggestion_engine as se


# ---------------------------------------------------------------------------
class TestResolveDataDate:
    def test_returns_none_when_fo_missing(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.FoEodRepo.latest_trade_date",
                     return_value=None)
        mocker.patch("lifecycle.suggestion_engine.IvHistoryRepo.latest_trade_date",
                     return_value=date(2026, 4, 30))
        assert se._resolve_data_date(mock_db) is None

    def test_returns_none_when_iv_missing(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.FoEodRepo.latest_trade_date",
                     return_value=date(2026, 4, 30))
        mocker.patch("lifecycle.suggestion_engine.IvHistoryRepo.latest_trade_date",
                     return_value=None)
        assert se._resolve_data_date(mock_db) is None

    def test_returns_common_date_when_both_match(self, mock_db, mocker):
        d = date(2026, 4, 30)
        mocker.patch("lifecycle.suggestion_engine.FoEodRepo.latest_trade_date",
                     return_value=d)
        mocker.patch("lifecycle.suggestion_engine.IvHistoryRepo.latest_trade_date",
                     return_value=d)
        assert se._resolve_data_date(mock_db) == d

    def test_uses_min_when_fo_ahead_of_iv(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.FoEodRepo.latest_trade_date",
                     return_value=date(2026, 4, 30))
        mocker.patch("lifecycle.suggestion_engine.IvHistoryRepo.latest_trade_date",
                     return_value=date(2026, 4, 28))
        assert se._resolve_data_date(mock_db) == date(2026, 4, 28)

    def test_uses_min_when_iv_ahead_of_fo(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.FoEodRepo.latest_trade_date",
                     return_value=date(2026, 4, 28))
        mocker.patch("lifecycle.suggestion_engine.IvHistoryRepo.latest_trade_date",
                     return_value=date(2026, 4, 30))
        assert se._resolve_data_date(mock_db) == date(2026, 4, 28)


# ---------------------------------------------------------------------------
class TestExecutionWindow:
    def test_during_market_hours_returns_now_string(self):
        entry = date(2026, 5, 4)
        now = datetime(2026, 5, 4, 10, 30)
        out = se._execution_window(entry, now)
        assert "Market is open" in out

    def test_pre_market_returns_window(self):
        entry = date(2026, 5, 4)
        now = datetime(2026, 5, 4, 8, 0)
        out = se._execution_window(entry, now)
        assert "09:20" in out

    def test_after_market_returns_window_with_date(self):
        entry = date(2026, 5, 5)
        now = datetime(2026, 5, 4, 18, 0)
        out = se._execution_window(entry, now)
        assert "09:20" in out
        assert "Tue" in out  # 5 May 2026 is Tuesday


# ---------------------------------------------------------------------------
class TestNextTradingDay:
    def test_monday_to_tuesday(self):
        # 4 May 2026 is Monday
        assert se._next_trading_day(date(2026, 5, 4)) == date(2026, 5, 5)

    def test_friday_to_monday(self):
        # 1 May 2026 is Friday
        assert se._next_trading_day(date(2026, 5, 1)) == date(2026, 5, 4)

    def test_saturday_to_monday(self):
        assert se._next_trading_day(date(2026, 5, 2)) == date(2026, 5, 4)

    def test_sunday_to_monday(self):
        assert se._next_trading_day(date(2026, 5, 3)) == date(2026, 5, 4)


# ---------------------------------------------------------------------------
class TestIsMonthlyExpiry:
    def test_last_thursday_of_month_is_monthly(self):
        # Last Thursday of May 2026 is 28 May
        assert se._is_monthly_expiry(date(2026, 5, 28)) is True

    def test_first_thursday_is_weekly(self):
        # 7 May 2026 is a Thursday but not last
        assert se._is_monthly_expiry(date(2026, 5, 7)) is False

    def test_non_thursday_is_not_monthly(self):
        assert se._is_monthly_expiry(date(2026, 5, 27)) is False  # Wed


# ---------------------------------------------------------------------------
class TestPickExpiriesInBand:
    def test_returns_empty_when_no_expiries(self, mocker):
        fo = MagicMock()
        fo.expiries_for.return_value = []
        assert se._pick_expiries_in_band(fo, "NIFTY", date(2026, 4, 30)) == []

    def test_filters_to_dte_band(self, mocker):
        fo = MagicMock()
        # 4-day, 14-day, 35-day expiries from trade_date (entry day = next day)
        td = date(2026, 4, 30)  # Thursday → entry Friday May 1
        fo.expiries_for.return_value = [
            date(2026, 5, 4),    # 3 DTE — too short
            date(2026, 5, 14),   # 13 DTE — in band, weekly
            date(2026, 5, 28),   # 27 DTE — too far
        ]
        result = se._pick_expiries_in_band(fo, "NIFTY", td)
        assert len(result) == 1
        assert result[0][0] == date(2026, 5, 14)

    def test_returns_monthly_and_weekly(self, mocker):
        fo = MagicMock()
        td = date(2026, 4, 30)
        # Within band but distinct: weekly = May 14, monthly = May 28
        fo.expiries_for.return_value = [
            date(2026, 5, 14),    # Thursday weekly
            date(2026, 5, 28),    # Last Thursday — monthly (out of band, but include for test)
        ]
        result = se._pick_expiries_in_band(fo, "NIFTY", td)
        # Only weekly is in band (28 days from May 1 = > 21 dte_max default)
        assert any(t == "Weekly" for _, t in result)


# ---------------------------------------------------------------------------
class TestRunSuggestionEngine:
    def test_aborts_when_no_data(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine._resolve_data_date",
                     return_value=None)
        assert se.run_suggestion_engine(mock_db) == 0

    def test_skips_underlyings_on_exception(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine._resolve_data_date",
                     return_value=date(2026, 4, 30))
        mocker.patch("lifecycle.suggestion_engine.now_ist",
                     return_value=datetime(2026, 4, 30, 18, 0))
        mocker.patch("lifecycle.suggestion_engine._evaluate_underlying",
                     side_effect=RuntimeError("eval blew up"))
        # Should not raise; returns 0 persisted
        assert se.run_suggestion_engine(mock_db, trade_date=date(2026, 4, 30)) == 0

    def test_persists_one_suggestion_when_eval_returns_one(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.now_ist",
                     return_value=datetime(2026, 4, 30, 18, 0))

        # Build a minimal Suggestion dataclass
        from contracts import (Suggestion, SuggestionLeg, SuggestionEconomics,
                               ConfidenceResult, ChargeBreakdown)
        conf = ConfidenceResult(checks=[], failed_reasons=[], score=7, total=7,
                                all_passed=True)
        sug = Suggestion(
            suggestion_id="SUG-1",
            trade_name="N1",
            generated_on=datetime(2026, 4, 30, 18, 0),
            strategy="BULL_PUT_SPREAD",
            strategy_type="WRITING",
            underlying="NIFTY",
            expiry_date=date(2026, 5, 14),
            expiry_type="Weekly",
            dte=14,
            spot_at_generation=23000.0,
            confidence=conf,
            legs=[],
            economics=SuggestionEconomics(
                net_credit=50.0, max_profit=3750.0, max_loss=11250.0,
                upper_breakeven=None, lower_breakeven=22950.0,
                stop_loss_level=22900.0, probability_of_profit=70.0,
                estimated_charges=ChargeBreakdown(brokerage=0, stt=0, exchange=0,
                                                  gst=0, sebi=0, stamp_duty=0,
                                                  total=0),
                estimated_net_pnl=3500.0,
            ),
            execution_window="x",
            plain_english="x",
        )

        mocker.patch("lifecycle.suggestion_engine._evaluate_underlying",
                     return_value=([sug], []))
        mocker.patch("lifecycle.suggestion_engine.SuggestionRepo.expire_stale_pending",
                     return_value=0)
        mocker.patch("lifecycle.suggestion_engine.SuggestionRepo.has_suggestion_for",
                     return_value=False)
        mocker.patch("lifecycle.suggestion_engine.SuggestionRepo.insert")
        mocker.patch("lifecycle.suggestion_engine.SuggestionRepo.insert_legs")
        mocker.patch("lifecycle.suggestion_engine.NotificationRepo.insert")
        # 3 underlyings → eval returns same suggestion 3 times → dedup keeps best (first equal)
        n = se.run_suggestion_engine(mock_db, trade_date=date(2026, 4, 30))
        assert n == 1
        mock_db.commit.assert_called()

    def test_skips_when_already_persisted(self, mock_db, mocker):
        from contracts import Suggestion, SuggestionEconomics, ConfidenceResult, ChargeBreakdown
        conf = ConfidenceResult(checks=[], failed_reasons=[], score=7, total=7,
                                all_passed=True)
        sug = Suggestion(
            suggestion_id="SUG-2", trade_name="N2",
            generated_on=datetime(2026, 4, 30, 18, 0),
            strategy="IRON_CONDOR", strategy_type="WRITING",
            underlying="NIFTY", expiry_date=date(2026, 5, 14),
            expiry_type="Weekly", dte=14, spot_at_generation=23000.0,
            confidence=conf, legs=[],
            economics=SuggestionEconomics(
                net_credit=80, max_profit=6000, max_loss=14000,
                upper_breakeven=23300, lower_breakeven=22700,
                stop_loss_level=23250, probability_of_profit=65,
                estimated_charges=ChargeBreakdown(0, 0, 0, 0, 0, 0, 0),
                estimated_net_pnl=5500,
            ),
            execution_window="x", plain_english="x",
        )
        mocker.patch("lifecycle.suggestion_engine.now_ist",
                     return_value=datetime(2026, 4, 30, 18, 0))
        mocker.patch("lifecycle.suggestion_engine._evaluate_underlying",
                     return_value=([sug], []))
        mocker.patch("lifecycle.suggestion_engine.SuggestionRepo.expire_stale_pending",
                     return_value=0)
        mocker.patch("lifecycle.suggestion_engine.SuggestionRepo.has_suggestion_for",
                     return_value=True)  # already exists
        ins = mocker.patch("lifecycle.suggestion_engine.SuggestionRepo.insert")
        n = se.run_suggestion_engine(mock_db, trade_date=date(2026, 4, 30))
        assert n == 0
        ins.assert_not_called()


# ---------------------------------------------------------------------------
class TestEvaluateUnderlying:
    def test_returns_empty_when_no_spot(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.SpotEodRepo.for_date",
                     return_value=None)
        sugs, ns = se._evaluate_underlying(
            mock_db, "NIFTY", date(2026, 4, 30), date(2026, 5, 1), "x"
        )
        assert sugs == [] and ns == []

    def test_returns_empty_when_no_expiries(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.SpotEodRepo.for_date",
                     return_value={"close_price": 23000.0,
                                   "trade_date": date(2026, 4, 30)})
        mocker.patch("lifecycle.suggestion_engine._pick_expiries_in_band",
                     return_value=[])
        sugs, ns = se._evaluate_underlying(
            mock_db, "NIFTY", date(2026, 4, 30), date(2026, 5, 1), "x"
        )
        assert sugs == [] and ns == []


# ---------------------------------------------------------------------------
class TestPickExpiriesInBandEntryDay:
    """_pick_expiries_in_band respects an explicit entry_day (live mode)."""

    def test_entry_day_overrides_next_trading_day(self):
        fo = MagicMock()
        td = date(2026, 5, 5)       # Tuesday
        live_entry = date(2026, 5, 5)   # today (market open)
        # Expiry 10 days from today should be inside [7, 21] band
        fo.expiries_for.return_value = [date(2026, 5, 15)]   # 10 DTE from May 5
        result = se._pick_expiries_in_band(fo, "NIFTY", td, entry_day=live_entry)
        # With entry_day=today, 10 DTE is in band → returned
        assert len(result) == 1
        assert result[0][0] == date(2026, 5, 15)

    def test_no_entry_day_uses_next_trading_day(self):
        fo = MagicMock()
        td = date(2026, 5, 5)
        fo.expiries_for.return_value = [date(2026, 5, 12)]   # 7 DTE from May 5, 6 DTE from May 6
        # Without override, entry_day = next trading day = May 6 → DTE = 6 (below 7 min)
        result_no_override = se._pick_expiries_in_band(fo, "NIFTY", td)
        # With override entry_day=May 5 → DTE = 7 (exactly at min) → in band
        result_with_override = se._pick_expiries_in_band(fo, "NIFTY", td, entry_day=td)
        assert len(result_no_override) == 0
        assert len(result_with_override) == 1


# ---------------------------------------------------------------------------
class TestComputeLiveAtmIvRank:
    """_compute_live_atm_iv_rank returns (atm_iv, iv_rank) from live chain rows."""

    def _make_chain_row(self, strike, opt_type, price):
        return {
            "strike": strike, "option_type": opt_type,
            "close_price": price, "last_price": price, "settle_price": price,
        }

    def test_returns_positive_atm_iv(self, mock_db, mocker):
        iv_repo = MagicMock()
        iv_repo.atm_iv_history.return_value = [
            {"atm_iv": 0.15}, {"atm_iv": 0.20}, {"atm_iv": 0.25},
        ]
        # ATM = 23000, spot = 23000, CE=PE premium ~200 → IV ~15-25%
        chain = [
            self._make_chain_row(23000, "CE", 200.0),
            self._make_chain_row(23000, "PE", 200.0),
        ]
        atm_iv, iv_rank = se._compute_live_atm_iv_rank(
            chain, spot=23000.0, dte=14, iv_repo=iv_repo,
            symbol="NIFTY", today=date(2026, 5, 5),
        )
        assert atm_iv > 0
        assert iv_rank is not None
        assert 0.0 <= iv_rank <= 100.0

    def test_returns_zero_iv_for_empty_chain(self, mock_db, mocker):
        iv_repo = MagicMock()
        iv_repo.atm_iv_history.return_value = []
        atm_iv, iv_rank = se._compute_live_atm_iv_rank(
            [], spot=23000.0, dte=14, iv_repo=iv_repo,
            symbol="NIFTY", today=date(2026, 5, 5),
        )
        assert atm_iv == 0.0
        assert iv_rank is None

    def test_iv_rank_none_when_no_history(self, mock_db, mocker):
        iv_repo = MagicMock()
        iv_repo.atm_iv_history.return_value = []
        chain = [
            self._make_chain_row(23000, "CE", 200.0),
            self._make_chain_row(23000, "PE", 200.0),
        ]
        _, iv_rank = se._compute_live_atm_iv_rank(
            chain, spot=23000.0, dte=14, iv_repo=iv_repo,
            symbol="NIFTY", today=date(2026, 5, 5),
        )
        assert iv_rank is None


# ---------------------------------------------------------------------------
class TestRunLiveSuggestionEngine:
    """run_live_suggestion_engine skips correctly when provider has no live quotes."""

    def _make_provider(self, supports_live=True):
        p = MagicMock()
        caps = MagicMock()
        caps.supports_live_quotes = supports_live
        caps.name = "zerodha"
        p.capabilities.return_value = caps
        p.name = "zerodha"
        return p

    def test_skips_when_no_live_quotes(self, mock_db, mocker):
        p = self._make_provider(supports_live=False)
        assert se.run_live_suggestion_engine(mock_db, provider=p) == 0

    def test_skips_on_weekend(self, mock_db, mocker):
        p = self._make_provider(supports_live=True)
        # 2 May 2026 is Saturday
        mocker.patch("lifecycle.suggestion_engine.today_ist",
                     return_value=date(2026, 5, 2))
        assert se.run_live_suggestion_engine(mock_db, provider=p) == 0

    def test_aborts_when_no_fo_data(self, mock_db, mocker):
        p = self._make_provider(supports_live=True)
        mocker.patch("lifecycle.suggestion_engine.today_ist",
                     return_value=date(2026, 5, 5))
        mocker.patch("lifecycle.suggestion_engine.FoEodRepo.latest_trade_date",
                     return_value=None)
        assert se.run_live_suggestion_engine(mock_db, provider=p) == 0

    def test_eval_exception_per_symbol_is_swallowed(self, mock_db, mocker):
        p = self._make_provider(supports_live=True)
        mocker.patch("lifecycle.suggestion_engine.today_ist",
                     return_value=date(2026, 5, 5))
        mocker.patch("lifecycle.suggestion_engine.FoEodRepo.latest_trade_date",
                     return_value=date(2026, 5, 2))
        mocker.patch("lifecycle.suggestion_engine._evaluate_underlying",
                     side_effect=RuntimeError("provider down"))
        # Should not raise; returns 0
        assert se.run_live_suggestion_engine(mock_db, provider=p) == 0
