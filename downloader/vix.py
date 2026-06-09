"""
downloader/vix.py
=================

Download India VIX history.

The configured `vix_archive_url` (niftyindices.com) was decommissioned in
2024 and now returns an HTML page instead of CSV. We therefore use NSE's
live `allIndices` API as the primary source — it returns the current day's
INDIA VIX OHLC. For a richer history, point `vix_archive_url` at a working
CSV endpoint (the legacy parser is preserved for that case).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import date, datetime
from typing import List, Optional

from config import NSE_CONFIG
from contracts import VixRow
from downloader.nse_session import fetch_with_retry, make_session
from utils import safe_float, today_ist

logger = logging.getLogger(__name__)

_NSE_ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"


def _parse_date(raw: str) -> date:
    raw = raw.strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unparseable VIX date: {raw!r}")


def _looks_like_csv(text: str) -> bool:
    head = text.lstrip()[:64].lower()
    return not head.startswith("<")


def _parse_rows(csv_text: str) -> List[VixRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("Empty VIX CSV")
    fields = {f.strip().lower(): f for f in reader.fieldnames}

    def col(*names: str) -> str:
        for n in names:
            if n in fields:
                return fields[n]
        raise KeyError(f"VIX CSV missing any of: {names}")

    c_date  = col("date")
    c_open  = col("open")
    c_high  = col("high")
    c_low   = col("low")
    c_close = col("close")

    out: List[VixRow] = []
    for raw in reader:
        try:
            d = _parse_date(raw[c_date])
            close = safe_float(raw[c_close])
            if close is None:
                continue
            out.append(VixRow(
                trade_date  = d,
                open_price  = safe_float(raw[c_open], close) or close,
                high_price  = safe_float(raw[c_high], close) or close,
                low_price   = safe_float(raw[c_low], close) or close,
                close_price = close,
            ))
        except Exception as exc:
            logger.debug("Skipping bad VIX row: %s", exc)
    return out


def _fetch_live_vix(session) -> Optional[VixRow]:
    """Read today's INDIA VIX OHLC from NSE's allIndices API."""
    resp = fetch_with_retry(session, _NSE_ALL_INDICES_URL, accept_404=True)
    if resp is None:
        return None
    try:
        payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("VIX live: non-JSON response (%s)", exc)
        return None
    for item in payload.get("data", []):
        name = (item.get("indexSymbol") or item.get("index") or "").strip().upper()
        if name == "INDIA VIX":
            close = safe_float(item.get("last")) or safe_float(item.get("previousClose"))
            if not close or close <= 0:
                return None
            opn  = safe_float(item.get("open"),  close) or close
            high = safe_float(item.get("high"),  close) or close
            low  = safe_float(item.get("low"),   close) or close
            return VixRow(
                trade_date  = today_ist(),
                open_price  = opn,
                high_price  = high,
                low_price   = low,
                close_price = close,
            )
    logger.warning("VIX live: 'INDIA VIX' not found in allIndices payload")
    return None


def bundled_vix_csv_path() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "hist_india_vix_-30-04-2025-to-30-04-2026.csv")
    )


def load_bundled_vix_rows() -> List[VixRow]:
    """Parse the repo-bundled India VIX history CSV (if present)."""
    csv_path = bundled_vix_csv_path()
    if not os.path.exists(csv_path):
        return []
    out: List[VixRow] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for rec in reader:
            raw_date = (rec.get("Date") or "").strip()
            if not raw_date:
                continue
            try:
                dt = _parse_date(raw_date)
            except ValueError:
                logger.debug("VIX bundled: unparseable date %r — skipping", raw_date)
                continue
            close = safe_float((rec.get("Close") or "").replace(",", ""))
            if close is None or close <= 0:
                continue
            opn = safe_float((rec.get("Open") or "").replace(",", ""), close) or close
            high = safe_float((rec.get("High") or "").replace(",", ""), close) or close
            low = safe_float((rec.get("Low") or "").replace(",", ""), close) or close
            out.append(VixRow(
                trade_date=dt,
                open_price=opn,
                high_price=high,
                low_price=low,
                close_price=close,
            ))
    return out


def download_vix_for_date(trade_date: date) -> List[VixRow]:
    """Return VIX OHLC for a single trade date (manual backfill / date override)."""
    for row in load_bundled_vix_rows():
        if row.trade_date == trade_date:
            logger.info("VIX bundled CSV: 1 row for %s", trade_date)
            return [row]

    session = make_session()
    if trade_date == today_ist():
        live = _fetch_live_vix(session)
        if live is not None and live.trade_date == trade_date:
            return [live]

    url = NSE_CONFIG.get("vix_archive_url")
    if url:
        try:
            resp = fetch_with_retry(session, url, accept_404=True)
            if resp is not None and _looks_like_csv(resp.text):
                matched = [r for r in _parse_rows(resp.text) if r.trade_date == trade_date]
                if matched:
                    logger.info("VIX archive: 1 row for %s", trade_date)
                    return matched
        except Exception as exc:
            logger.warning("VIX archive fetch for %s failed (%s)", trade_date, exc)

    return []


def download_vix_history() -> List[VixRow]:
    session = make_session()

    # 1) Try the configured archive CSV first (covers historical backfill).
    url = NSE_CONFIG.get("vix_archive_url")
    if url:
        logger.info("Downloading VIX history: %s", url)
        try:
            resp = fetch_with_retry(session, url, accept_404=True)
            if resp is not None and _looks_like_csv(resp.text):
                rows = _parse_rows(resp.text)
                if rows:
                    logger.info("VIX archive parsed: %d rows", len(rows))
                    return rows
                logger.warning("VIX archive parsed 0 rows, falling back to live API")
            else:
                logger.warning("VIX archive endpoint returned non-CSV (likely HTML); "
                               "falling back to live API")
        except Exception as exc:
            logger.warning("VIX archive fetch failed (%s); falling back to live API", exc)

    # 2) Fall back to live API (today's value only).
    live = _fetch_live_vix(session)
    if live is None:
        logger.warning("VIX: no rows available from any source")
        return []
    logger.info("VIX live API: 1 row for %s (close=%.2f)", live.trade_date, live.close_price)
    return [live]
