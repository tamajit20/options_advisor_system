"""Tests for Scout index group tags."""

from scout.index_groups import index_tags, sort_watchlist_rows


def test_index_tags_nifty50():
    assert "nifty50" in index_tags("RELIANCE")
    assert "nifty50" in index_tags("TCS")


def test_index_tags_nifty_bank():
    tags = index_tags("HDFCBANK")
    assert "nifty50" in tags
    assert "nifty_bank" in tags


def test_index_tags_other():
    assert index_tags("UNKNOWNXYZ") == []


def test_sort_watchlist_rows_nifty50_first():
    rows = [
        {"symbol": "ZOMATO", "index_tags": []},
        {"symbol": "RELIANCE", "index_tags": ["nifty50"]},
        {"symbol": "AUBANK", "index_tags": ["nifty_bank"]},
    ]
    out = sort_watchlist_rows(rows)
    assert [r["symbol"] for r in out] == ["RELIANCE", "AUBANK", "ZOMATO"]
