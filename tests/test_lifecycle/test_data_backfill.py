"""Tests for lifecycle/data_backfill.py."""
from __future__ import annotations

from datetime import date

from lifecycle import data_backfill as bf
from exceptions import NoDataError


class TestDatesToProcess:
    def test_includes_missing_weekdays_and_today(self):
        have = {date(2026, 6, 10), date(2026, 6, 12)}
        end = date(2026, 6, 16)  # Tuesday

        def has_date(d: date) -> bool:
            return d in have

        dates = bf.dates_to_process(
            has_date=has_date,
            end=end,
            lookback_days=10,
            always_refresh_end=True,
        )
        assert date(2026, 6, 11) in dates   # missing Wed
        assert date(2026, 6, 16) in dates  # refresh today
        assert date(2026, 6, 10) not in dates  # present, not end

    def test_skips_weekends(self):
        end = date(2026, 6, 14)  # Sunday
        dates = bf.dates_to_process(
            has_date=lambda d: False,
            end=end,
            lookback_days=3,
            always_refresh_end=True,
        )
        assert end not in dates


class TestRunDatesBackfill:
    def test_aggregates_rows_and_skips_holiday(self):
        calls = []

        def worker(d: date) -> int:
            calls.append(d)
            if d == date(2026, 6, 11):
                raise NoDataError("holiday")
            return 10

        total = bf.run_dates_backfill(
            [date(2026, 6, 11), date(2026, 6, 12)],
            worker,
            label="test",
            today=date(2026, 6, 16),
        )
        assert total == 10
        assert calls == [date(2026, 6, 11), date(2026, 6, 12)]

    def test_raises_when_today_has_no_data(self):
        def worker(d: date) -> int:
            raise NoDataError("not published")

        try:
            bf.run_dates_backfill(
                [date(2026, 6, 16)],
                worker,
                label="test",
                today=date(2026, 6, 16),
            )
            assert False, "expected NoDataError"
        except NoDataError:
            pass
