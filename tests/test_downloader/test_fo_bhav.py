"""Tests for downloader/fo_bhav.py — CSV parsing, instrument code mapping, filters."""
from __future__ import annotations

import io
import zipfile
from datetime import date

import pytest

from downloader.fo_bhav import _extract_csv_text, _parse_rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_HEADER = ",".join([
    "TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
    "OpnPric", "HghPric", "LwPric", "ClsPric", "SttlmPric",
    "TtlTradgVol", "OpnIntrst", "ChngInOpnIntrst",
])


def _row(
    instr: str = "OPTIDX",
    sym: str = "NIFTY",
    xpry: str = "14-MAY-2026",
    strike: str = "23000",
    opt: str = "CE",
    op: str = "100", hp: str = "110", lp: str = "90", cp: str = "105", sp: str = "105",
    vol: str = "1000", oi: str = "50000", chg: str = "100",
    trad: str = "30-APR-2026",
) -> str:
    return ",".join([trad, instr, sym, xpry, strike, opt, op, hp, lp, cp, sp, vol, oi, chg])


def _csv(*rows: str) -> str:
    return _HEADER + "\n" + "\n".join(rows) + "\n"


TRADE_DATE = date(2026, 4, 30)


# ---------------------------------------------------------------------------
class TestParseRows:
    def test_happy_path_optidx(self):
        rows = _parse_rows(_csv(_row()), TRADE_DATE)
        assert len(rows) == 1
        r = rows[0]
        assert r.symbol == "NIFTY"
        assert r.instrument == "OPTIDX"
        assert r.option_type == "CE"
        assert r.strike == 23000.0
        assert r.expiry_date == date(2026, 5, 14)
        assert r.close_price == 105.0
        assert r.open_interest == 50000

    def test_legacy_codes_mapped(self):
        """OPTIDX/OPTSTK pass through unchanged."""
        rows = _parse_rows(_csv(_row(instr="OPTSTK", sym="RELIANCE")), TRADE_DATE)
        assert rows[0].instrument == "OPTSTK"

    def test_new_codes_ido_sto_mapped_to_legacy(self):
        """NSE 2024+ codes IDO/STO must map to OPTIDX/OPTSTK."""
        rows = _parse_rows(_csv(_row(instr="IDO"), _row(instr="STO", sym="TCS")), TRADE_DATE)
        instruments = {r.instrument for r in rows}
        assert instruments == {"OPTIDX", "OPTSTK"}

    def test_futures_filtered_out(self):
        """FUTIDX / FUTSTK / FF rows must be dropped."""
        rows = _parse_rows(_csv(_row(instr="FUTIDX"), _row(instr="FUTSTK"), _row()), TRADE_DATE)
        assert len(rows) == 1  # only the OPTIDX row survives

    def test_invalid_option_type_filtered(self):
        rows = _parse_rows(_csv(_row(opt="XX"), _row(opt="")), TRADE_DATE)
        assert rows == []

    def test_missing_strike_skipped(self):
        rows = _parse_rows(_csv(_row(strike="")), TRADE_DATE)
        assert rows == []

    def test_missing_required_column_raises(self):
        bad_header = "TradDt,TckrSymb,XpryDt,StrkPric,OptnTp\n30-APR-2026,NIFTY,14-MAY-2026,23000,CE\n"
        with pytest.raises(ValueError, match="missing required columns"):
            _parse_rows(bad_header, TRADE_DATE)

    def test_empty_csv_raises(self):
        with pytest.raises(ValueError, match="Empty"):
            _parse_rows("", TRADE_DATE)

    def test_malformed_row_skipped_not_raised(self):
        """Bad strike value should not abort the batch."""
        good = _row()
        bad = _row(strike="not-a-number")
        rows = _parse_rows(_csv(good, bad), TRADE_DATE)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
class TestExtractCsvText:
    def test_reads_first_csv_in_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("foo.csv", "hello,world\n")
        out = _extract_csv_text(buf.getvalue())
        assert "hello,world" in out

    def test_zip_without_csv_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("foo.txt", "no csv here")
        with pytest.raises(ValueError, match="no CSV"):
            _extract_csv_text(buf.getvalue())
