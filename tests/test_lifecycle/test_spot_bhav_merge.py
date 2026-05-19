from datetime import date

from contracts import SpotBhavRow
from lifecycle.spot_bhav_merge import merge_spot_bhav_rows


def test_index_ohlc_not_overwritten_by_fo():
    td = date(2026, 4, 30)
    index = SpotBhavRow(td, "NIFTY", 23000, 23100, 22900, 23050, 0)
    rows = merge_spot_bhav_rows([], [index], {"NIFTY": 99999.0}, td)
    assert len(rows) == 1
    assert rows[0].close_price == 23050.0
    assert rows[0].high_price == 23100.0


def test_fo_fallback_when_no_index():
    td = date(2026, 4, 30)
    rows = merge_spot_bhav_rows([], [], {"NIFTY": 23000.0}, td)
    assert rows[0].close_price == 23000.0
    assert rows[0].high_price == rows[0].low_price
