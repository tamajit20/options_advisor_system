"""Tests for lifecycle/suggestion_engine.py — internal helpers + run() orchestration."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock

import pytest

from lifecycle import suggestion_engine as se


# ---------------------------------------------------------------------------
class TestExceedsMaxLossCap:
    """Tail-risk ceiling: veto when a single 1-lot position's max loss breaches
    max_loss_pct_of_capital × capital. Backs the May-2026 straddle fix where
    ₹60-70k debit positions (12-14% of ₹500k) lost multiples of their SL."""

    def test_over_concentrated_position_exceeds(self):
        # 70k max loss vs 10% of 500k (=50k) → blocked.
        assert se.exceeds_max_loss_cap(70_000.0, 500_000.0, 0.10) is True

    def test_properly_sized_position_passes(self):
        # 44k max loss vs 50k ceiling → allowed (the winning straddles).
        assert se.exceeds_max_loss_cap(44_000.0, 500_000.0, 0.10) is False

    def test_exactly_at_ceiling_passes(self):
        assert se.exceeds_max_loss_cap(50_000.0, 500_000.0, 0.10) is False

    def test_disabled_when_pct_zero(self):
        assert se.exceeds_max_loss_cap(70_000.0, 500_000.0, 0.0) is False

    def test_disabled_when_capital_zero(self):
        assert se.exceeds_max_loss_cap(70_000.0, 0.0, 0.10) is False

    def test_disabled_when_max_loss_unknown(self):
        assert se.exceeds_max_loss_cap(0.0, 500_000.0, 0.10) is False


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
        # 1 May 2026 is Friday (also Maharashtra Day). Next session is Monday 4 May.
        assert se._next_trading_day(date(2026, 5, 1)) == date(2026, 5, 4)

    def test_saturday_to_monday(self):
        assert se._next_trading_day(date(2026, 5, 2)) == date(2026, 5, 4)

    def test_sunday_to_monday(self):
        assert se._next_trading_day(date(2026, 5, 3)) == date(2026, 5, 4)

    def test_skips_maharashtra_day(self):
        # Thursday 30 Apr → skip Friday 1 May holiday and weekend → Monday 4 May
        assert se._next_trading_day(date(2026, 4, 30)) == date(2026, 5, 4)

    def test_skips_ganesh_chaturthi(self):
        # Friday 11 Sep → skip weekend and Monday 14 Sep holiday → Tuesday 15 Sep
        assert se._next_trading_day(date(2026, 9, 11)) == date(2026, 9, 15)


# ---------------------------------------------------------------------------
class TestIsMonthlyExpiry:
    def test_last_thursday_of_month_is_monthly(self):
        # Last Thursday of May 2026 is 28 May
        assert se._is_monthly_expiry(date(2026, 5, 28)) is True

    def test_first_thursday_is_weekly(self):
        # 7 May 2026 is a Thursday but not last
        assert se._is_monthly_expiry(date(2026, 5, 7)) is False

    def test_non_thursday_is_not_monthly_without_catalogue(self):
        assert se._is_monthly_expiry(date(2026, 5, 27)) is False  # Wed

    def test_holiday_shifted_wednesday_is_monthly_with_catalogue(self):
        """Bug 11: last F&O expiry of month need not be Thursday."""
        catalogue = [
            date(2026, 5, 7),
            date(2026, 5, 14),
            date(2026, 5, 21),
            date(2026, 5, 27),  # holiday-shifted monthly (Wed)
        ]
        assert se._is_monthly_expiry(date(2026, 5, 27), catalogue) is True
        assert se._is_monthly_expiry(date(2026, 5, 21), catalogue) is False
        assert se._expiry_type_label(date(2026, 5, 27), catalogue) == "Monthly"


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

    def test_falls_back_to_nearest_when_none_in_band(self, mocker):
        fo = MagicMock()
        td = date(2026, 9, 22)  # Tue → entry Wed Sep 23
        fo.expiries_for.return_value = [
            date(2026, 9, 29),   # 6 DTE — just inside-short
            date(2026, 10, 27),  # 34 DTE — far monthly
        ]
        result = se._pick_expiries_in_band(
            fo, "BANKNIFTY", td, entry_day=date(2026, 9, 23),
        )
        assert len(result) == 1
        assert result[0][0] == date(2026, 9, 29)


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
        persist = mocker.patch("lifecycle.suggestion_engine._persist_and_notify",
                               return_value=0)
        # Should not raise; sit-out rows are recorded per symbol
        assert se.run_suggestion_engine(mock_db, trade_date=date(2026, 4, 30)) == 0
        nss = persist.call_args.args[2]
        assert {n.underlying for n in nss} == {"NIFTY", "BANKNIFTY", "FINNIFTY"}
        assert all("Evaluation failed" in n.reason for n in nss)

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


class TestExpiryDteSitOutReason:
    def test_none_when_in_band(self):
        reason = se.expiry_dte_sit_out_reason(
            [date(2026, 5, 14)], date(2026, 5, 1), dte_min=7, dte_max=21,
        )
        assert reason is None

    def test_reason_lists_nearest_when_out_of_band(self):
        reason = se.expiry_dte_sit_out_reason(
            [date(2026, 5, 4), date(2026, 5, 28)],
            date(2026, 5, 1),
            dte_min=7,
            dte_max=21,
        )
        assert reason is not None
        assert "7–21 DTE band" in reason
        assert "2026-05-04 (3 DTE)" in reason
        assert "2026-05-28 (27 DTE)" in reason

    def test_none_when_no_expiries(self):
        assert se.expiry_dte_sit_out_reason([], date(2026, 5, 1)) is None


# ---------------------------------------------------------------------------
class TestEvaluateUnderlying:
    def test_returns_empty_when_no_spot(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.SpotEodRepo.for_date",
                     return_value=None)
        sugs, ns = se._evaluate_underlying(
            mock_db, "NIFTY", date(2026, 4, 30), date(2026, 5, 1), "x"
        )
        assert sugs == []
        assert len(ns) == 1
        assert ns[0].underlying == "NIFTY"
        assert "No spot price" in ns[0].reason

    def test_returns_empty_when_no_expiries(self, mock_db, mocker):
        mocker.patch("lifecycle.suggestion_engine.SpotEodRepo.for_date",
                     return_value={"close_price": 23000.0,
                                   "trade_date": date(2026, 4, 30)})
        mocker.patch("lifecycle.suggestion_engine._pick_expiries_in_band",
                     return_value=[])
        mocker.patch("lifecycle.suggestion_engine.FoEodRepo.expiries_for",
                     return_value=[date(2026, 5, 4), date(2026, 5, 28)])
        sugs, ns = se._evaluate_underlying(
            mock_db, "BANKNIFTY", date(2026, 4, 30), date(2026, 5, 1), "x"
        )
        assert sugs == []
        assert len(ns) == 1
        assert ns[0].underlying == "BANKNIFTY"
        assert "DTE band" in ns[0].reason
        assert "2026-05-04" in ns[0].reason

    def _patch_eval_to_confidence(self, mocker, *, trend: str):
        from contracts import ConfidenceCheck, ConfidenceResult, MarketIndicators

        mocker.patch(
            "lifecycle.suggestion_engine.SpotEodRepo.for_date",
            return_value={"close_price": 23000.0, "trade_date": date(2026, 4, 30)},
        )
        mocker.patch(
            "lifecycle.suggestion_engine._pick_expiries_in_band",
            return_value=[(date(2026, 5, 14), "Weekly")],
        )
        mocker.patch("lifecycle.suggestion_engine.SpotEodRepo.history", return_value=[])
        mocker.patch("lifecycle.suggestion_engine.VixRepo.history", return_value=[])
        mocker.patch(
            "lifecycle.suggestion_engine.EventCalendarRepo.has_high_impact",
            return_value=False,
        )
        mocker.patch(
            "lifecycle.suggestion_engine.EventCalendarRepo.first_high_impact_event",
            return_value=None,
        )
        mocker.patch("lifecycle.suggestion_engine.EventCalendarRepo.count_all", return_value=0)
        mocker.patch("lifecycle.suggestion_engine.LotSizeRepo.for_symbol", return_value=75)
        mocker.patch("lifecycle.suggestion_engine.TradeRepo.open_trades", return_value=[])
        mocker.patch("lifecycle.suggestion_engine.FiiRepo.for_date", return_value=[])
        mocker.patch(
            "lifecycle.suggestion_engine.IvHistoryRepo.latest_for",
            return_value=[{
                "expiry_date": date(2026, 5, 14),
                "atm_iv": 0.18,
                "iv_rank": None,
            }],
        )
        mocker.patch(
            "lifecycle.suggestion_engine.FoEodRepo.get_chain",
            return_value=[{"strike": 23000.0, "option_type": "CE", "close_price": 100.0}],
        )
        mocker.patch("lifecycle.suggestion_engine.stamp_eod_rows")
        mocker.patch(
            "lifecycle.suggestion_engine.SuggestionRepo.next_suggestion_id",
            return_value="SUG-1",
        )
        mocker.patch(
            "lifecycle.suggestion_engine.build_indicators",
            return_value=MarketIndicators(
                symbol="NIFTY", as_of=date(2026, 4, 30), spot=23000.0,
                pcr=1.0, max_pain=23000.0, atr_14=150.0, trend=trend,
                vix_close=15.0, vix_regime="STABLE",
                oi_walls_call=[], oi_walls_put=[], expected_move=300.0,
                hv_20=0.12, iv_premium=1.2, fii_net_futures=0.0,
            ),
        )
        mocker.patch(
            "lifecycle.suggestion_engine.evaluate_confidence",
            return_value=ConfidenceResult(
                score=9, total=9, all_passed=True,
                checks=[ConfidenceCheck(label="t", status="PASS", detail="ok")],
                failed_reasons=[],
            ),
        )

    def test_sideways_without_iv_rank_records_no_suggestion(self, mock_db, mocker):
        self._patch_eval_to_confidence(mocker, trend="SIDEWAYS")
        sugs, ns = se._evaluate_underlying(
            mock_db, "NIFTY", date(2026, 4, 30), date(2026, 5, 1), "x",
        )
        assert sugs == []
        assert ns
        assert any("IV rank unavailable" in n.reason for n in ns)

    def test_mixed_trend_sits_out_not_sideways_condor(self, mock_db, mocker):
        self._patch_eval_to_confidence(mocker, trend="MIXED")
        sugs, ns = se._evaluate_underlying(
            mock_db, "NIFTY", date(2026, 4, 30), date(2026, 5, 1), "x",
        )
        assert sugs == []
        assert ns
        assert any("Mixed trend" in n.reason for n in ns)
        assert not any("IV rank unavailable" in n.reason for n in ns)

    def test_directional_without_iv_rank_records_strategy_veto(self, mock_db, mocker):
        self._patch_eval_to_confidence(mocker, trend="BULLISH")
        sugs, ns = se._evaluate_underlying(
            mock_db, "NIFTY", date(2026, 4, 30), date(2026, 5, 1), "x",
        )
        assert sugs == []
        assert ns
        assert any("IV rank unavailable" in n.reason for n in ns)


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
        # Without override, entry_day = next trading day = May 6 → DTE = 6 (below 7)
        result_no_override = se._pick_expiries_in_band(fo, "NIFTY", td)
        # With override entry_day=May 5 → DTE = 7 (exactly at min) → in band
        result_with_override = se._pick_expiries_in_band(fo, "NIFTY", td, entry_day=td)
        assert result_with_override[0][0] == date(2026, 5, 12)
        # Out-of-band is no longer dropped — nearest expiry is still evaluated
        assert result_no_override[0][0] == date(2026, 5, 12)


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
        persist = mocker.patch("lifecycle.suggestion_engine._persist_and_notify",
                               return_value=0)
        # Should not raise; sit-out rows are recorded per symbol
        assert se.run_live_suggestion_engine(mock_db, provider=p) == 0
        nss = persist.call_args.args[2]
        assert {n.underlying for n in nss} == {"NIFTY", "BANKNIFTY", "FINNIFTY"}
        assert all("Evaluation failed" in n.reason for n in nss)


class TestIcIbCompanions:
    def test_skips_non_ic_ib(self, mocker):
        sized = mocker.patch("lifecycle.suggestion_engine._assemble_sized_suggestion")
        primary = MagicMock(strategy="BULL_CALL_SPREAD")
        se._append_ic_ib_companions(
            primary=primary,
            suggestions=[],
            existing_names=[],
            assemble_kw={"underlying": "NIFTY"},
            sug_repo=MagicMock(),
            id_date=date(2026, 5, 4),
            provenance=None,
            db=MagicMock(),
        )
        sized.assert_not_called()

    def test_builds_bps_and_bcs_for_iron_condor(self, mocker):
        mocker.patch(
            "lifecycle.suggestion_engine._attach_em_calibration_warning",
        )
        bps = MagicMock(trade_name="N-BPS")
        bcs = MagicMock(trade_name="N-BCS")
        sized = mocker.patch(
            "lifecycle.suggestion_engine._assemble_sized_suggestion",
            side_effect=[bps, bcs],
        )
        repo = MagicMock()
        repo.next_suggestion_id.side_effect = ["SUG-C1", "SUG-C2"]
        out = []
        names = ["N-IC"]
        primary = MagicMock(
            strategy="IRON_CONDOR",
            trade_name="N-IC",
            underlying="NIFTY",
            expiry_date=date(2026, 5, 14),
            legs=[MagicMock(lots=2), MagicMock(lots=2), MagicMock(lots=2), MagicMock(lots=2)],
        )
        se._append_ic_ib_companions(
            primary=primary,
            suggestions=out,
            existing_names=names,
            assemble_kw={"underlying": "NIFTY", "lot_size": 75},
            sug_repo=repo,
            id_date=date(2026, 5, 4),
            provenance=None,
            db=MagicMock(),
        )
        assert [s.trade_name for s in out] == ["N-BPS", "N-BCS"]
        assert names == ["N-IC", "N-BPS", "N-BCS"]
        overrides = [
            c.kwargs.get("strategy_override") for c in sized.call_args_list
        ]
        assert overrides == ["BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"]
        # Companions must go through capital sizing, not copy primary lots.
        for c in sized.call_args_list:
            assert c.kwargs.get("assemble_kw", {}).get("companion_mode") is True
            assert "lots" not in c.kwargs.get("assemble_kw", {})

