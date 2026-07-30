"""Tests for downloader/vix.py — date parsing, CSV parsing."""
from __future__ import annotations

from datetime import date

import pytest

from downloader.vix import (
    _parse_date,
    _parse_rows,
    _parse_vix_from_index_close_csv,
    download_vix_for_date,
    load_bundled_vix_rows,
)

_INDEX_CLOSE_SAMPLE = """\
Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield
Nifty 50,29-07-2026,23985.35,24000.0,23900.0,23950.0,-35.35,-.15,1,1,-,-,-
India VIX,29-07-2026,12.56,12.80,12.10,12.45,-0.11,-.88,-,-,-,-,-
"""


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


class TestParseVixFromIndexCloseCsv:
    def test_extracts_india_vix_row(self):
        row = _parse_vix_from_index_close_csv(_INDEX_CLOSE_SAMPLE, date(2026, 7, 29))
        assert row is not None
        assert row.trade_date == date(2026, 7, 29)
        assert row.close_price == 12.45
        assert row.open_price == 12.56
        assert row.high_price == 12.80
        assert row.low_price == 12.10

    def test_missing_vix_returns_none(self):
        csv = "Index Name,Closing Index Value\nNifty 50,24000\n"
        assert _parse_vix_from_index_close_csv(csv, date(2026, 7, 29)) is None


class TestDownloadVixForDate:
    def test_loads_from_bundled_csv_when_date_present(self):
        rows = load_bundled_vix_rows()
        if not rows:
            pytest.skip("bundled VIX CSV not present in workspace")
        sample = rows[0].trade_date
        got = download_vix_for_date(sample)
        assert len(got) == 1
        assert got[0].trade_date == sample

    def test_missing_date_returns_empty_without_network(self, monkeypatch):
        monkeypatch.setattr("downloader.vix.load_bundled_vix_rows", lambda: [])
        monkeypatch.setattr("downloader.vix.today_ist", lambda: date(2099, 1, 1))
        monkeypatch.setattr("downloader.vix._fetch_vix_from_nse_index_close", lambda *_a, **_k: None)
        monkeypatch.setattr("downloader.vix.NSE_CONFIG", {"vix_archive_url": None})
        assert download_vix_for_date(date(2000, 1, 1)) == []

    def test_backfills_from_nse_index_close_when_bundled_missing(self, monkeypatch):
        monkeypatch.setattr("downloader.vix.load_bundled_vix_rows", lambda: [])
        target = date(2026, 7, 29)
        from contracts import VixRow
        expected = VixRow(
            trade_date=target,
            open_price=12.56,
            high_price=12.80,
            low_price=12.10,
            close_price=12.45,
        )
        monkeypatch.setattr(
            "downloader.vix._fetch_vix_from_nse_index_close",
            lambda *_a, **_k: expected,
        )
        got = download_vix_for_date(target)
        assert got == [expected]
