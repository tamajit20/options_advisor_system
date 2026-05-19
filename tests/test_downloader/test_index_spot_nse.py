"""Tests for NSE index close CSV parsing."""
from __future__ import annotations

from datetime import date

from downloader.index_spot_nse import _parse_rows

_HEADER = "Index Name,Open,High,Low,Close"
_CSV = _HEADER + "\n" + "NIFTY 50,23000,23100,22900,23050\n" + "NIFTY BANK,50000,50100,49900,50050\n"


def test_parse_nifty_and_banknifty():
    rows = _parse_rows(_CSV, date(2026, 4, 30), keep_only={"NIFTY", "BANKNIFTY"})
    assert len(rows) == 2
    by = {r.symbol: r for r in rows}
    assert by["NIFTY"].close_price == 23050.0
    assert by["NIFTY"].high_price > by["NIFTY"].low_price
    assert by["BANKNIFTY"].symbol == "BANKNIFTY"
