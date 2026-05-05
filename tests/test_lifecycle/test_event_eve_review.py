"""Tests for lifecycle/event_eve_review.py (Phase 3 — #5)."""
from __future__ import annotations

from datetime import date

import pytest

from lifecycle import event_eve_review as eer


_TODAY = date(2026, 5, 4)
_TOMORROW = date(2026, 5, 5)


class TestRunEventEveReview:
    def test_no_event_tomorrow_inserts_nothing(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "has_high_impact", return_value=False,
        )
        open_trades_mock = mocker.patch.object(
            eer.TradeRepo, "open_trades", return_value=[],
        )
        n = eer.run_event_eve_review(mock_db, today=_TODAY)
        assert n == 0
        open_trades_mock.assert_not_called()
        mock_db.commit.assert_not_called()

    def test_inserts_one_per_active_trade(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "has_high_impact", return_value=True,
        )
        mocker.patch.object(
            eer.EventCalendarRepo, "first_high_impact_event",
            return_value={
                "event_date": _TOMORROW,
                "event_type": "FOMC",
                "description": "Fed rate decision",
            },
        )
        mocker.patch.object(
            eer.TradeRepo, "open_trades",
            return_value=[
                {"trade_id": "T-1", "trade_name": "NIFTY-IC",   "status": "ACTIVE"},
                {"trade_id": "T-2", "trade_name": "BANK-CONDOR","status": "ACTIVE"},
                # Pending close should be skipped — only ACTIVE counts.
                {"trade_id": "T-3", "trade_name": "FIN-PUT",    "status": "PENDING_CLOSE"},
            ],
        )
        insert_mock = mocker.patch.object(eer.NotificationRepo, "insert")

        n = eer.run_event_eve_review(mock_db, today=_TODAY)

        assert n == 2
        assert insert_mock.call_count == 2
        # Verify the body mentions the event description.
        first_body = insert_mock.call_args_list[0][0][0].body
        assert "Fed rate decision" in first_body
        assert _TOMORROW.isoformat() in first_body
        mock_db.commit.assert_called_once()

    def test_no_active_trades_no_inserts(self, mock_db, mocker):
        mocker.patch.object(
            eer.EventCalendarRepo, "has_high_impact", return_value=True,
        )
        mocker.patch.object(
            eer.EventCalendarRepo, "first_high_impact_event",
            return_value={"event_type": "BUDGET", "description": "Union Budget"},
        )
        mocker.patch.object(eer.TradeRepo, "open_trades", return_value=[])
        insert_mock = mocker.patch.object(eer.NotificationRepo, "insert")
        n = eer.run_event_eve_review(mock_db, today=_TODAY)
        assert n == 0
        insert_mock.assert_not_called()
