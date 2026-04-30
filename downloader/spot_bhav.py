"""
downloader/spot_bhav.py
=======================

Download NSE Cash-Market EOD bhav copy.

We only need a handful of underlyings (NIFTY, BANKNIFTY, FINNIFTY, plus stocks
that appear in the F&O list). The CSV contains all listed equities; we filter
post-parse.

Boundary: returns `List[SpotBhavRow]`. NO DB writes here.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import zipfile
from datetime import date
from typing import Iterable, List, Optional, Set

from config import NSE_CONFIG, PATHS, STRATEGY_CONFIG
from contracts import SpotBhavRow
from downloader.nse_session import fetch_with_retry, make_session
from utils import fmt_yyyymmdd, safe_float, safe_int

logger = logging.getLogger(__name__)


def _build_url(trade_date: date) -> str:
    return NSE_CONFIG["spot_bhav_url"].format(yyyymmdd=fmt_yyyymmdd(trade_date))


def _archive_path(trade_date: date) -> str:
    d = os.path.join(PATHS["archive_dir"], "spot_bhav")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"spot_bhav_{fmt_yyyymmdd(trade_date)}.zip")


def _extract_csv(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("Spot bhav zip contains no CSV file")
        with zf.open(names[0]) as fh:
            return fh.read().decode("utf-8", errors="replace")


# Index symbols don't appear in cash-market bhav under that name. We accept
# their proxy index names from the NSE CSV (`TckrSymb` = NIFTY 50, NIFTY BANK,
# NIFTY FIN SERVICE) and rename them to our canonical symbols.
_INDEX_ALIASES = {
    "NIFTY 50":         "NIFTY",
    "NIFTY BANK":       "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
}


def _parse_rows(
    csv_text: str,
    trade_date: date,
    keep_only: Optional[Set[str]] = None,
) -> List[SpotBhavRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("Empty spot bhav CSV")

    out: List[SpotBhavRow] = []
    for raw in reader:
        try:
            sym_raw = (raw.get("TckrSymb") or "").strip().upper()
            sym = _INDEX_ALIASES.get(sym_raw, sym_raw)
            if not sym:
                continue
            if keep_only is not None and sym not in keep_only:
                continue
            series = (raw.get("SctySrs") or "").strip().upper()
            # Equity series only or index entries (which have no SctySrs)
            if series and series not in ("EQ", "BE", ""):
                continue
            row = SpotBhavRow(
                trade_date  = trade_date,
                symbol      = sym,
                open_price  = safe_float(raw.get("OpnPric"), 0.0) or 0.0,
                high_price  = safe_float(raw.get("HghPric"), 0.0) or 0.0,
                low_price   = safe_float(raw.get("LwPric"), 0.0) or 0.0,
                close_price = safe_float(raw.get("ClsPric"), 0.0) or 0.0,
                volume      = safe_int(raw.get("TtlTradgVol"), 0) or 0,
            )
            out.append(row)
        except Exception as exc:
            logger.debug("Skipping bad spot row: %s", exc)
    return out


def download_spot_bhav(
    trade_date: date,
    *,
    archive: bool = True,
    keep_only: Optional[Iterable[str]] = None,
) -> List[SpotBhavRow]:
    if keep_only is None:
        # Default: just the configured underlyings
        keep_only = set(STRATEGY_CONFIG["underlyings"])
    keep_set: Set[str] = {s.upper() for s in keep_only}

    url = _build_url(trade_date)
    logger.info("Downloading spot bhav %s: %s", trade_date, url)
    session = make_session()
    resp = fetch_with_retry(session, url, accept_404=True)
    if resp is None:
        logger.warning("Spot bhav not available for %s (404)", trade_date)
        return []
    zip_bytes = resp.content

    if archive:
        try:
            with open(_archive_path(trade_date), "wb") as fh:
                fh.write(zip_bytes)
        except OSError as exc:
            logger.warning("Could not archive spot bhav zip: %s", exc)

    csv_text = _extract_csv(zip_bytes)
    rows = _parse_rows(csv_text, trade_date, keep_only=keep_set)
    logger.info("Spot bhav %s parsed: %d rows (filtered)", trade_date, len(rows))
    return rows
