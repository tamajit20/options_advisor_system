"""Tests for lifecycle/event_eve_review.py (morning PRE_EVENT_EXIT + banner items)."""
from __future__ import annotations

from datetime import date

import pytest

from lifecycle import event_eve_review as eer


_TODAY = date(2026, 5, 4)
_TOMORROW = date(2026, 5, 5)


class TestIsShortPremium:
    def test_credit_strategies(self):
        assert eer.is_short_premium_trade(strategy="IRON_CONDOR") is True
        assert eer.is_short_premium_trade(strategy="BULL_PUT_SPREAD") is True
        assert eer.is_short_premium_trade(strategy="JADE_LIZARD") is True

    def test_long_premium_false(self):
        assert eer.is_short_premium_trade(strategy="LONG_PUT") is False
        assert eer.is_short_premium_trade(strategy="BEAR_PUT_SPREAD") is False

    def test_writing_type_or_positive_credit(self):
        assert eer.is_short_premium_trade(
            strategy=None, strategy_type="WRITING",
        ) is True
        assert eer.is_short_premium_trade(
            strategy=None, net_credit_actual=1200.0,
        ) is True


class TestRunEventEveReview:
    def test_no_event_tomorrow_inserts_nothing(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "high_impact_events", return_value=[],
        )
        open_trades_mock = mocker.patch.object(
            eer.TradeRepo, "open_trades", return_value=[],
        )
        n = eer.run_event_eve_review(mock_db, today=_TODAY)
        assert n == 0
        open_trades_mock.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_short_gets_pre_event_exit_critical(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "high_impact_events",
            return_value=[{
                "event_date": _TOMORROW,
                "event_type": "FOMC",
                "description": "Fed rate decision",
            }],
        )
        mocker.patch.object(
            eer.TradeRepo, "open_trades",
            return_value=[
                {
                    "trade_id": "T-1",
                    "trade_name": "NIFTY-IC",
                    "status": "ACTIVE",
                    "suggestion_id": "SUG-1",
                    "net_credit_actual": 800,
                },
                {
                    "trade_id": "T-2",
                    "trade_name": "NIFTY-PUT",
                    "status": "ACTIVE",
                    "suggestion_id": "SUG-2",
                },
                {
                    "trade_id": "T-3",
                    "trade_name": "FIN-PUT",
                    "status": "PENDING_CLOSE",
                    "suggestion_id": "SUG-3",
                },
            ],
        )
        mocker.patch.object(
            eer.SuggestionRepo, "get",
            side_effect=lambda sid: {
                "SUG-1": {"strategy": "IRON_CONDOR", "strategy_type": "WRITING"},
                "SUG-2": {"strategy": "LONG_PUT", "strategy_type": "BUYING"},
                "SUG-3": {"strategy": "IRON_CONDOR", "strategy_type": "WRITING"},
            }.get(sid),
        )
        mock_db.scalar.return_value = 0
        insert_mock = mocker.patch.object(eer.NotificationRepo, "insert")

        n = eer.run_event_eve_review(mock_db, today=_TODAY)

        assert n == 2
        types = [c[0][0].notif_type for c in insert_mock.call_args_list]
        assert "PRE_EVENT_EXIT" in types
        assert "EVENT_AHEAD_REVIEW" in types
        pre = next(c[0][0] for c in insert_mock.call_args_list
                   if c[0][0].notif_type == "PRE_EVENT_EXIT")
        assert pre.severity == "CRITICAL"
        assert "close tonight" in pre.title.lower() or "close tonight" in pre.body.lower()
        assert "Fed rate decision" in pre.body
        assert _TOMORROW.isoformat() in pre.body
        mock_db.commit.assert_called_once()

    def test_skips_duplicate_same_day(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "high_impact_events",
            return_value=[{"event_type": "RBI", "description": "RBI MPC",
                           "event_date": _TOMORROW}],
        )
        mocker.patch.object(
            eer.TradeRepo, "open_trades",
            return_value=[{
                "trade_id": "T-1",
                "trade_name": "IC",
                "status": "ACTIVE",
                "suggestion_id": "SUG-1",
            }],
        )
        mocker.patch.object(
            eer.SuggestionRepo, "get",
            return_value={"strategy": "IRON_CONDOR", "strategy_type": "WRITING"},
        )
        mock_db.scalar.return_value = 1
        insert_mock = mocker.patch.object(eer.NotificationRepo, "insert")
        n = eer.run_event_eve_review(mock_db, today=_TODAY)
        assert n == 0
        insert_mock.assert_not_called()

    def test_no_active_trades_no_inserts(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "high_impact_events",
            return_value=[{"event_type": "BUDGET", "description": "Union Budget",
                           "event_date": _TOMORROW}],
        )
        mocker.patch.object(eer.TradeRepo, "open_trades", return_value=[])
        insert_mock = mocker.patch.object(eer.NotificationRepo, "insert")
        n = eer.run_event_eve_review(mock_db, today=_TODAY)
        assert n == 0
        insert_mock.assert_not_called()


class TestPreEventBanner:
    def test_banner_none_without_event(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "high_impact_events", return_value=[],
        )
        assert eer.build_pre_event_exit_banner(mock_db, today=_TODAY) is None

    def test_banner_one_item_per_event(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "high_impact_events",
            return_value=[
                {
                    "event_date": _TOMORROW,
                    "event_type": "US_FOMC",
                    "description": "Fed rate decision",
                },
                {
                    "event_date": _TOMORROW,
                    "event_type": "CPI_RELEASE",
                    "description": "US CPI",
                },
            ],
        )
        mocker.patch.object(
            eer.TradeRepo, "open_trades",
            return_value=[{
                "trade_id": "T-1",
                "trade_name": "NIFTY-IC",
                "status": "ACTIVE",
                "suggestion_id": "SUG-1",
                "net_credit_actual": 500,
            }],
        )
        mocker.patch.object(
            eer.SuggestionRepo, "get",
            return_value={"strategy": "IRON_CONDOR", "strategy_type": "WRITING"},
        )
        banner = eer.build_pre_event_exit_banner(mock_db, today=_TODAY)
        assert banner is not None
        assert banner["active"] is True
        assert len(banner["items"]) == 2
        keys = {it["event_key"] for it in banner["items"]}
        assert len(keys) == 2
        assert all(it["when"] == "tomorrow" for it in banner["items"])
        assert "NIFTY-IC" in banner["items"][0]["trade_names"]
        assert "PRE-EVENT EXIT" in banner["items"][0]["message"]
