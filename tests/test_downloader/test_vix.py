"""Tests for downloader/vix.py — date parsing, CSV parsing."""
from __future__ import annotations

from datetime import date

import pytest

from downloader.vix import _parse_date, _parse_rows


class TestParseDate:
    @pytest.mark.parametrize("raw,expected", [
        ("30-Apr-2026", date(2026, 4, 30)),
        ("30-APR-2026", date(2026, 4, 30)),
        ("30-Apr-26",   date(2026, 4, 30)),
        ("2026-04-30",  date(2026, 4, 30)),
        ("30/04/2026",  date(2026, 4, 30)),
    ])
    def test_supported_formats(self, raw, expected):
        assert _parse_date(raw) == expected

    def test_strips_whitespace(self):
        assert _parse_date("  30-Apr-2026  ") == date(2026, 4, 30)

    def test_unparseable_raises(self):
        with pytest.raises(ValueError, match="Unparseable"):
            _parse_date("not-a-date")


class TestParseRows:
    def test_happy_path(self):
        csv = "Date,Open,High,Low,Close\n30-Apr-2026,15.0,15.5,14.8,15.2\n"
        rows = _parse_rows(csv)
        assert len(rows) == 1
        r = rows[0]
        assert r.trade_date == date(2026, 4, 30)
        assert r.close_price == 15.2

    def test_missing_close_skipped(self):
        csv = "Date,Open,High,Low,Close\n30-Apr-2026,15.0,15.5,14.8,\n"
        rows = _parse_rows(csv)
        assert rows == []

    def test_case_insensitive_headers(self):
        csv = "DATE,OPEN,HIGH,LOW,CLOSE\n30-Apr-2026,15,15.5,14.8,15.2\n"
        rows = _parse_rows(csv)
        assert len(rows) == 1

    def test_open_falls_back_to_close_when_blank(self):
        csv = "Date,Open,High,Low,Close\n30-Apr-2026,,15.5,14.8,15.2\n"
        rows = _parse_rows(csv)
        assert rows[0].open_price == 15.2

    def test_empty_csv_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            _parse_rows("")

    def test_missing_required_column_raises(self):
        with pytest.raises(KeyError, match="missing"):
            _parse_rows("Date,Open\n30-Apr-2026,15\n")

    def test_multiple_rows(self):
        csv = (
            "Date,Open,High,Low,Close\n"
            "29-Apr-2026,14.5,14.8,14.0,14.7\n"
            "30-Apr-2026,15.0,15.5,14.8,15.2\n"
        )
        rows = _parse_rows(csv)
        assert len(rows) == 2
        assert rows[0].trade_date == date(2026, 4, 29)
