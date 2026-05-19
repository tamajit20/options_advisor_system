"""
downloader/index_spot_nse.py
============================

NSE index EOD OHLC from the daily ``ind_close_all`` archive CSV.

Indices (NIFTY, BANKNIFTY, FINNIFTY) are not present in the cash-market
bhav with usable OHLC; this file is the primary EOD source for index spot
history used by trend / ATR / HV-20.

Boundary: returns ``List[SpotBhavRow]``. NO DB writes.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date
from typing import Iterable, List, Optional, Set

from config import NSE_CONFIG
from contracts import SpotBhavRow
from downloader.nse_session import fetch_with_retry, make_session
from utils import safe_float

logger = logging.getLogger(__name__)

# NSE ``Index Name`` column → canonical symbol
_INDEX_ALIASES = {
    "NIFTY 50":          "NIFTY",
    "NIFTY BANK":        "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY MID SELECT":  "MIDCPNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
}


def _build_url(trade_date: date) -> str:
    ddmmyyyy = trade_date.strftime("%d%m%Y")
    return NSE_CONFIG["index_close_url"].format(ddmmyyyy=ddmmyyyy)


def _normalise_header(h: str) -> str:
    return (h or "").strip().upper().replace(" ", "_")


def _parse_rows(
    csv_text: str,
    trade_date: date,
    keep_only: Optional[Set[str]] = None,
) -> List[SpotBhavRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return []

    # Map normalised header → original key
    col_map = {_normalise_header(h): h for h in reader.fieldnames}

    def _col(*candidates: str) -> Optional[str]:
        for c in candidates:
            k = col_map.get(c)
            if k:
                return k
        return None

    name_col = _col("INDEX_NAME", "INDEX", "INDEXNAME")
    open_col = _col("OPEN", "OPEN_INDEX", "OPEN_INDEX_VAL")
    high_col = _col("HIGH", "HIGH_INDEX", "HIGH_INDEX_VAL")
    low_col  = _col("LOW", "LOW_INDEX", "LOW_INDEX_VAL")
    close_col = _col("CLOSE", "CLOSING", "CLOSE_INDEX", "CLOSE_INDEX_VAL", "CLOSING_INDEX_VAL")

    if not name_col or not close_col:
        logger.warning(
            "index_close CSV %s: missing name/close columns (headers=%s)",
            trade_date, list(reader.fieldnames)[:8],
        )
        return []

    out: List[SpotBhavRow] = []
    for raw in reader:
        try:
            sym_raw = (raw.get(name_col) or "").strip().upper()
            sym = _INDEX_ALIASES.get(sym_raw, sym_raw)
            if not sym:
                continue
            if keep_only is not None and sym not in keep_only:
                continue
            close = safe_float(raw.get(close_col), 0.0) or 0.0
            if close <= 0:
                continue
            o = safe_float(raw.get(open_col), 0.0) if open_col else 0.0
            h = safe_float(raw.get(high_col), 0.0) if high_col else 0.0
            l = safe_float(raw.get(low_col), 0.0) if low_col else 0.0
            o = o or close
            h = h or close
            l = l or close
            if h < l:
                h, l = l, h
            out.append(SpotBhavRow(
                trade_date=trade_date,
                symbol=sym,
                open_price=o,
                high_price=h,
                low_price=l,
                close_price=close,
                volume=0,
            ))
        except Exception as exc:
            logger.debug("Skipping bad index row: %s", exc)
    return out


def download_nse_index_spot(
    trade_date: date,
    *,
    keep_only: Optional[Iterable[str]] = None,
) -> List[SpotBhavRow]:
    """Fetch index OHLC for ``trade_date``. Returns [] on 404 or parse failure."""
    keep_set: Optional[Set[str]] = (
        {s.upper() for s in keep_only} if keep_only is not None else None
    )
    url = _build_url(trade_date)
    logger.info("Downloading NSE index close %s: %s", trade_date, url)
    session = make_session()
    resp = fetch_with_retry(session, url, accept_404=True)
    if resp is None:
        logger.warning("NSE index close not available for %s (404)", trade_date)
        return []
    return _parse_rows(resp.text, trade_date, keep_only=keep_set)
