"""Tests for lifecycle/no_data_messages.py."""
from __future__ import annotations

from datetime import date

import pytest

from lifecycle.no_data_messages import (
    enrich_with_latest_in_db,
    format_no_data_message,
    latest_available_suffix,
)


class TestFormatNoDataMessage:
    def test_includes_latest_date_when_known(self):
        msg = format_no_data_message(
            dataset="FO bhavcopy",
            trade_date=date(2026, 7, 30),
            reason="market holiday or NSE has not published the file yet",
            latest_available=date(2026, 7, 29),
        )
        assert "FO bhavcopy not available for 2026-07-30" in msg
        assert "Latest available in DB: 2026-07-29" in msg

    def test_notes_empty_db(self):
        msg = format_no_data_message(
            dataset="VIX data",
            trade_date=date(2026, 7, 30),
            reason="NSE may not have published today's VIX data yet",
            latest_available=None,
        )
        assert "No data in DB yet" in msg


class TestLatestAvailableSuffix:
    def test_with_date(self):
        assert "2026-07-28" in latest_available_suffix(date(2026, 7, 28))

    def test_without_date(self):
        assert "No data in DB yet" in latest_available_suffix(None)


class TestEnrichWithLatestInDb:
    def test_appends_latest_for_known_job(self, monkeypatch):
        monkeypatch.setattr(
            "lifecycle.no_data_messages.latest_trade_date_for_job",
            lambda _db, _job: date(2026, 7, 29),
        )
        out = enrich_with_latest_in_db(
            None,
            "fo_bhav_download",
            "FO bhavcopy not available for 2026-07-30 — holiday.",
        )
        assert "Latest available in DB: 2026-07-29" in out

    def test_does_not_duplicate_suffix(self, monkeypatch):
        monkeypatch.setattr(
            "lifecycle.no_data_messages.latest_trade_date_for_job",
            lambda _db, _job: date(2026, 7, 28),
        )
        original = "Already has Latest available in DB: 2026-07-28."
        assert enrich_with_latest_in_db(None, "fo_bhav_download", original) == original


class TestClarifyMorningNoData:
    def test_rewrites_wrong_date_morning_window(self):
        from datetime import datetime

        msg = (
            "FO bhavcopy not available for 2026-08-04 — "
            "market holiday or NSE has not published the file yet"
        )
        started = datetime(2026, 8, 4, 9, 0, 54)
        out = __import__(
            "lifecycle.no_data_messages", fromlist=["clarify_morning_no_data_message"]
        ).clarify_morning_no_data_message(
            msg, job_name="fo_bhav_download", started_at=started,
        )
        assert "2026-08-03" in out
        assert "prior trading session" in out
        assert "morning pre-market run" in out
        assert "2026-08-04" not in out

    def test_leaves_evening_message_unchanged(self):
        from datetime import datetime

        msg = (
            "FO bhavcopy not available for 2026-08-04 — "
            "market holiday or NSE has not published the file yet"
        )
        started = datetime(2026, 8, 4, 20, 35, 0)
        out = __import__(
            "lifecycle.no_data_messages", fromlist=["clarify_morning_no_data_message"]
        ).clarify_morning_no_data_message(
            msg, job_name="fo_bhav_download", started_at=started,
        )
        assert out == msg
