"""Tests for downloader/spot_bhav.py — index alias mapping, EQ-series filtering."""
from __future__ import annotations

from datetime import date

from downloader.spot_bhav import _parse_rows


_HEADER = "TradDt,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol"


def _csv(*rows: str) -> str:
    return _HEADER + "\n" + "\n".join(rows) + "\n"


def _r(sym: str, sctysrs: str = "EQ", close: str = "100") -> str:
    return f"30-APR-2026,{sym},{sctysrs},99,101,98,{close},1000"


TRADE_DATE = date(2026, 4, 30)


class TestParseRows:
    def test_index_aliases_renamed(self):
        rows = _parse_rows(_csv(_r("NIFTY 50", "", "23000")), TRADE_DATE)
        assert len(rows) == 1
        assert rows[0].symbol == "NIFTY"

    def test_banknifty_alias(self):
        rows = _parse_rows(_csv(_r("NIFTY BANK", "", "50000")), TRADE_DATE)
        assert rows[0].symbol == "BANKNIFTY"

    def test_finnifty_alias(self):
        rows = _parse_rows(_csv(_r("NIFTY FIN SERVICE", "", "22000")), TRADE_DATE)
        assert rows[0].symbol == "FINNIFTY"

    def test_keep_only_filter(self):
        csv = _csv(_r("RELIANCE"), _r("TCS"), _r("INFY"))
        rows = _parse_rows(csv, TRADE_DATE, keep_only={"RELIANCE", "TCS"})
        symbols = {r.symbol for r in rows}
        assert symbols == {"RELIANCE", "TCS"}

    def test_non_eq_series_filtered_out(self):
        """SctySrs='SM' (SME), 'IT' (debt), etc. should be dropped."""
        rows = _parse_rows(_csv(_r("RELIANCE", "EQ"), _r("XYZ", "SM"), _r("ABC", "BE")),
                           TRADE_DATE)
        symbols = {r.symbol for r in rows}
        assert "RELIANCE" in symbols
        assert "ABC" in symbols  # BE allowed
        assert "XYZ" not in symbols

    def test_empty_symbol_skipped(self):
        rows = _parse_rows(_csv(_r("", "EQ")), TRADE_DATE)
        assert rows == []

    def test_close_price_parsed(self):
        rows = _parse_rows(_csv(_r("RELIANCE", "EQ", "2500.50")), TRADE_DATE)
        assert rows[0].close_price == 2500.50
