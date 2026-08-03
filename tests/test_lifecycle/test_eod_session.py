"""Tests for lifecycle/eod_session.py and morning catchup date targeting."""
from __future__ import annotations

from datetime import date

from lifecycle import data_backfill as bf
from lifecycle.eod_session import (
    bhav_unavailable_reason,
    eod_pipeline_session,
    effective_bhav_end_date,
    is_morning_catchup,
    upstream_missing_reason,
)
from utils import previous_trading_day


class TestPreviousTradingDay:
    def test_monday_returns_friday(self):
        assert previous_trading_day(date(2026, 8, 3)) == date(2026, 7, 31)

    def test_tuesday_returns_monday(self):
        assert previous_trading_day(date(2026, 8, 4)) == date(2026, 8, 3)

    def test_saturday_input_returns_friday(self):
        assert previous_trading_day(date(2026, 8, 2)) == date(2026, 7, 31)


class TestEodPipelineSession:
    def test_evening_session_targets_today(self):
        with eod_pipeline_session(morning_catchup=False) as sess:
            assert sess.bhav_end_date == date.today()
            assert not is_morning_catchup()
            assert effective_bhav_end_date() == date.today()

    def test_morning_session_targets_prior_weekday(self, monkeypatch):
        monkeypatch.setattr(
            "lifecycle.eod_session.today_ist",
            lambda: date(2026, 8, 3),  # Monday
        )
        with eod_pipeline_session(morning_catchup=True) as sess:
            assert sess.bhav_end_date == date(2026, 7, 31)
            assert is_morning_catchup()
            assert effective_bhav_end_date() == date(2026, 7, 31)

    def test_session_cleared_after_context(self, monkeypatch):
        monkeypatch.setattr(
            "lifecycle.eod_session.today_ist",
            lambda: date(2026, 8, 3),
        )
        with eod_pipeline_session(morning_catchup=True):
            pass
        assert effective_bhav_end_date() == date.today()


class TestMorningMessages:
    def test_morning_bhav_reason_mentions_prior_session(self, monkeypatch):
        monkeypatch.setattr(
            "lifecycle.eod_session.today_ist",
            lambda: date(2026, 8, 3),
        )
        with eod_pipeline_session(morning_catchup=True):
            msg = bhav_unavailable_reason()
        assert "prior trading session" in msg
        assert "2026-07-31" in msg
        assert "today's bhav is not expected" in msg

    def test_upstream_missing_reason_morning(self, monkeypatch):
        monkeypatch.setattr(
            "lifecycle.eod_session.today_ist",
            lambda: date(2026, 8, 3),
        )
        with eod_pipeline_session(morning_catchup=True):
            msg = upstream_missing_reason("fo_bhav_download")
        assert "prior-session" in msg
        assert "2026-07-31" in msg


class TestMorningDatesToProcess:
    def test_monday_morning_does_not_include_monday(self, monkeypatch):
        monkeypatch.setattr(
            "lifecycle.eod_session.today_ist",
            lambda: date(2026, 8, 3),
        )
        with eod_pipeline_session(morning_catchup=True):
            dates = bf.dates_to_process(
                has_date=lambda d: d == date(2026, 7, 31),
                always_refresh_end=True,
            )
        assert date(2026, 8, 3) not in dates
        assert date(2026, 7, 31) in dates


class TestMorningCatchupEntryDay:
    def test_morning_catchup_uses_today_not_next_day(self, monkeypatch):
        from lifecycle.suggestion_engine import run_suggestion_engine
        from lifecycle import suggestion_engine as se_mod

        captured: dict = {}

        def fake_persist(db, all_candidates, no_suggestions, *, trade_date, entry_day, **kwargs):
            captured["entry_day"] = entry_day
            return 0

        monkeypatch.setattr(
            "lifecycle.eod_session.today_ist",
            lambda: date(2026, 8, 3),  # Monday
        )
        monkeypatch.setattr(
            se_mod, "_resolve_data_date",
            lambda db: date(2026, 7, 31),
        )
        monkeypatch.setattr(se_mod, "_persist_and_notify", fake_persist)
        monkeypatch.setattr(se_mod, "STRATEGY_CONFIG", {"underlyings": []})

        with __import__(
            "lifecycle.eod_session", fromlist=["eod_pipeline_session"]
        ).eod_pipeline_session(morning_catchup=True):
            run_suggestion_engine(object())

        assert captured["entry_day"] == date(2026, 8, 3)
