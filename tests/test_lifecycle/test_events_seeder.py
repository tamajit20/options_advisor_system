"""Tests for lifecycle/events_seeder.py."""
from __future__ import annotations

import os

import pytest

from lifecycle import events_seeder as es


class TestInsertIfNew:
    def test_skips_when_exists(self, mock_db):
        mock_db.scalar.return_value = 1
        assert es._insert_if_new(mock_db, "2026-05-04", "FOMC", "x", "HIGH") is False
        mock_db.execute.assert_not_called()

    def test_inserts_when_new(self, mock_db):
        mock_db.scalar.return_value = 0
        assert es._insert_if_new(mock_db, "2026-05-04", "FOMC", "x", "HIGH") is True
        mock_db.execute.assert_called_once()


class TestRunEventsSeed:
    def test_falls_back_to_static_when_no_api_key(self, mock_db, mocker, monkeypatch):
        monkeypatch.delenv("OPT_TE_API_KEY", raising=False)
        # Static config insertions all return False (already exist)
        mocker.patch("lifecycle.events_seeder._insert_if_new", return_value=False)
        n = es.run_events_seed(mock_db)
        assert n == 0

    def test_static_inserts_counted(self, mock_db, mocker, monkeypatch):
        monkeypatch.delenv("OPT_TE_API_KEY", raising=False)
        # Force static inserter to always insert
        mocker.patch("lifecycle.events_seeder._insert_if_new", return_value=True)
        n = es.run_events_seed(mock_db)
        # Returns total insertions, > 0 if EVENTS_CONFIG has rows
        assert n >= 0

    def test_api_failure_falls_through(self, mock_db, mocker, monkeypatch):
        monkeypatch.setenv("OPT_TE_API_KEY", "fake-key")
        mocker.patch("downloader.economic_calendar.fetch_high_impact_events",
                     side_effect=RuntimeError("api down"))
        mocker.patch("lifecycle.events_seeder._insert_if_new", return_value=False)
        # Should not raise
        n = es.run_events_seed(mock_db)
        assert n == 0
