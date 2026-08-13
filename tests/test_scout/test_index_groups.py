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
        {"symbol": "ZOMATO", "name": "Zomato", "index_tags": []},
        {"symbol": "RELIANCE", "name": "Reliance", "index_tags": ["nifty50"]},
        {"symbol": "AUBANK", "name": "AU Bank", "index_tags": ["nifty_bank"]},
    ]
    out = sort_watchlist_rows(rows)
    assert [r["symbol"] for r in out] == ["RELIANCE", "AUBANK", "ZOMATO"]


def test_sort_watchlist_rows_nifty50_index_order():
    rows = [
        {"symbol": "TCS", "name": "TCS", "index_tags": ["nifty50"]},
        {"symbol": "RELIANCE", "name": "Reliance", "index_tags": ["nifty50"]},
        {"symbol": "ADANIENT", "name": "Adani", "index_tags": ["nifty50"]},
    ]
    out = sort_watchlist_rows(rows)
    assert [r["symbol"] for r in out] == ["ADANIENT", "RELIANCE", "TCS"]


def test_sort_watchlist_rows_other_by_company_name():
    rows = [
        {"symbol": "360ONE", "name": "360 ONE WAM", "index_tags": []},
        {"symbol": "ABB", "name": "ABB India", "index_tags": []},
        {"symbol": "3MINDIA", "name": "3M India", "index_tags": []},
    ]
    out = sort_watchlist_rows(rows)
    assert [r["symbol"] for r in out] == ["360ONE", "3MINDIA", "ABB"]
