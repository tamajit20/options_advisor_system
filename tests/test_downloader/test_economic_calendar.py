"""Tests for downloader/economic_calendar.py — event classification + API fetch."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from downloader.economic_calendar import _classify_event, fetch_high_impact_events


class TestClassifyEvent:
    @pytest.mark.parametrize("name,country,expected", [
        ("Fed Interest Rate Decision",       "united states", "US_FOMC"),
        ("Interest Rate Decision",           "united states", "US_FOMC"),
        ("RBI Interest Rate Decision",       "india",         "RBI_MPC"),
        ("Interest Rate Decision",           "india",         "RBI_MPC"),
        ("Union Budget",                     "india",         "UNION_BUDGET"),
        ("GDP Growth Rate",                  "india",         "GDP_RELEASE"),
        ("CPI",                              "india",         "CPI_RELEASE"),
        ("Inflation Rate",                   "united states", "CPI_RELEASE"),
        ("Some Random Event",                "india",         "MACRO_EVENT"),
    ])
    def test_event_type_mapping(self, name, country, expected):
        assert _classify_event(name, country) == expected


class TestFetchHighImpactEvents:
    def test_no_api_key_returns_empty(self):
        assert fetch_high_impact_events("") == []

    def test_returns_only_high_importance(self, mocker):
        """Importance != 3 should be filtered out."""
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = [
            {"Date": "2026-05-10T09:00:00", "Event": "RBI Interest Rate Decision",
             "Importance": 3, "Country": "India"},
            {"Date": "2026-05-12T12:00:00", "Event": "Manufacturing PMI",
             "Importance": 1, "Country": "India"},  # filtered out
        ]
        mocker.patch("downloader.economic_calendar.requests.get", return_value=fake_resp)
        events = fetch_high_impact_events(
            "fake-key", start=date(2026, 5, 1), end=date(2026, 5, 31),
        )
        # Only Importance==3 events kept (one per fetched country = 2 calls)
        assert all(e["impact"] == "HIGH" for e in events)
        # Both fetched countries return same dataset → expect at least one HIGH event
        assert len(events) >= 1
        assert events[0]["event_type"] == "RBI_MPC"

    def test_api_failure_returns_empty_not_raises(self, mocker):
        mocker.patch(
            "downloader.economic_calendar.requests.get",
            side_effect=Exception("network down"),
        )
        # Should not raise — returns empty list
        out = fetch_high_impact_events("key")
        assert out == []
