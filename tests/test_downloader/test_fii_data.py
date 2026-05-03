"""Tests for downloader/fii_data.py — multi-line header detection, client filtering."""
from __future__ import annotations

from datetime import date

from downloader.fii_data import _parse_rows


_FII_CSV = """Some preamble row that is not data
Another comment row
Client Type,Future Index Long,Future Index Short,Option Index Call Long,Option Index Call Short,Option Index Put Long,Option Index Put Short
FII,100000,80000,50000,45000,30000,28000
DII,40000,42000,15000,14000,12000,11000
PRO,5000,6000,2000,2100,1800,1900
CLIENT,30000,29000,18000,17500,15000,14500
TOTAL,175000,157000,85000,78600,58800,55400
"""

TRADE_DATE = date(2026, 4, 30)


class TestParseRows:
    def test_extracts_canonical_clients(self):
        rows = _parse_rows(_FII_CSV, TRADE_DATE)
        clients = {r.client_type for r in rows}
        assert clients == {"FII", "DII", "PRO", "CLIENT"}

    def test_total_row_filtered_out(self):
        rows = _parse_rows(_FII_CSV, TRADE_DATE)
        assert all(r.client_type != "TOTAL" for r in rows)

    def test_field_values_parsed(self):
        rows = _parse_rows(_FII_CSV, TRADE_DATE)
        fii = next(r for r in rows if r.client_type == "FII")
        assert fii.future_long == 100000
        assert fii.future_short == 80000
        assert fii.option_call_long == 50000
        assert fii.option_put_short == 28000

    def test_trade_date_assigned(self):
        rows = _parse_rows(_FII_CSV, TRADE_DATE)
        assert all(r.trade_date == TRADE_DATE for r in rows)

    def test_no_header_returns_empty(self):
        """Missing 'Client Type' header → empty (graceful, logs warning)."""
        rows = _parse_rows("foo,bar\n1,2\n", TRADE_DATE)
        assert rows == []

    def test_empty_csv(self):
        assert _parse_rows("", TRADE_DATE) == []
