"""
downloader/fii_data.py
======================

Download NSE participant-wise OI (FII / DII / Pro / Client) for a given date.

The CSV format is awkward — it has a multi-line header. We parse defensively
and take the four canonical client types.

Boundary: returns `List[FiiOiRow]`. Graceful: missing FII data is non-fatal
(the suggestion engine treats the absence as a warning, not a hard fail).
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date
from typing import List

from config import NSE_CONFIG
from contracts import FiiOiRow
from downloader.nse_session import fetch_with_retry, make_session
from utils import fmt_ddmmyyyy, safe_int

logger = logging.getLogger(__name__)

_CANONICAL_CLIENTS = {"FII", "DII", "PRO", "CLIENT"}


def _build_url(trade_date: date) -> str:
    return NSE_CONFIG["fii_oi_url"].format(ddmmyyyy=fmt_ddmmyyyy(trade_date))


def _parse_rows(csv_text: str, trade_date: date) -> List[FiiOiRow]:
    out: List[FiiOiRow] = []
    # Find the data line(s) — they begin with "Client Type"-keyed table or
    # the header may be at line 1. We iterate raw rows and skip non-data.
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return out

    # Locate header row containing "Client Type"
    header_idx = None
    for i, r in enumerate(rows):
        joined = " ".join(c.strip().lower() for c in r if c)
        if "client type" in joined and "future" in joined:
            header_idx = i
            break
    if header_idx is None:
        logger.warning("FII CSV header not found; skipping")
        return out

    headers = [c.strip().lower() for c in rows[header_idx]]

    def find(*candidates: str) -> int:
        for c in candidates:
            for i, h in enumerate(headers):
                if c in h:
                    return i
        return -1

    i_client = find("client type")
    i_fl = find("future index long", "future stock long", "future long")
    i_fs = find("future index short", "future stock short", "future short")
    i_cl = find("option index call long", "call long")
    i_cs = find("option index call short", "call short")
    i_pl = find("option index put long", "put long")
    i_ps = find("option index put short", "put short")

    if i_client < 0:
        logger.warning("FII CSV missing 'Client Type' column")
        return out

    for r in rows[header_idx + 1:]:
        if len(r) <= i_client:
            continue
        ct = (r[i_client] or "").strip().upper()
        if ct not in _CANONICAL_CLIENTS:
            continue

        def get(idx: int) -> int:
            if idx < 0 or idx >= len(r):
                return 0
            return safe_int(r[idx], 0) or 0

        out.append(FiiOiRow(
            trade_date        = trade_date,
            client_type       = ct,
            future_long       = get(i_fl),
            future_short      = get(i_fs),
            option_call_long  = get(i_cl),
            option_call_short = get(i_cs),
            option_put_long   = get(i_pl),
            option_put_short  = get(i_ps),
        ))
    return out


def download_fii_oi(trade_date: date) -> List[FiiOiRow]:
    url = _build_url(trade_date)
    logger.info("Downloading FII participant OI %s: %s", trade_date, url)
    session = make_session()
    resp = fetch_with_retry(session, url, accept_404=True)
    if resp is None:
        logger.warning("FII OI not available for %s (404)", trade_date)
        return []
    rows = _parse_rows(resp.text, trade_date)
    logger.info("FII participant OI %s parsed: %d rows", trade_date, len(rows))
    return rows
