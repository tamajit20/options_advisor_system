"""
downloader/fo_bhav.py
=====================

Download NSE F&O EOD bhav copy and parse into `FoBhavRow` contracts.

Boundary:
    * NO DB writes here. Returns `List[FoBhavRow]`. The orchestrator (scheduler
      job) decides what to do with them (typically `FoEodRepo.upsert_many`).
    * Filters to `OPTIDX` and `OPTSTK` only — futures are dropped.

Schema notes (NSE 2024+ format):
    Columns of interest:
        TradDt, BizDt, Sgmt, Src, FinInstrmTp, FinInstrmId, ISIN,
        TckrSymb, SctySrs, XpryDt, FininstrmActlXpryDt, StrkPric, OptnTp,
        FinInstrmNm, OpnPric, HghPric, LwPric, ClsPric, LastPric,
        PrvsClsgPric, UndrlygPric, SttlmPric, OpnIntrst, ChngInOpnIntrst,
        TtlTradgVol, TtlTrfVal, TtlNbOfTxsExctd, ...
"""

from __future__ import annotations

import csv
import io
import logging
import os
import zipfile
from datetime import date
from typing import List, Optional

from config import NSE_CONFIG, PATHS
from contracts import FoBhavRow
from downloader.nse_session import fetch_with_retry, make_session
from utils import fmt_yyyymmdd, parse_nse_expiry, safe_float, safe_int

logger = logging.getLogger(__name__)


_REQUIRED_COLS = (
    "TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
    "OpnPric", "HghPric", "LwPric", "ClsPric", "SttlmPric",
    "TtlTradgVol", "OpnIntrst", "ChngInOpnIntrst",
)


def _build_url(trade_date: date) -> str:
    return NSE_CONFIG["fo_bhav_url"].format(yyyymmdd=fmt_yyyymmdd(trade_date))


def _archive_path(trade_date: date) -> str:
    archive_dir = os.path.join(PATHS["archive_dir"], "fo_bhav")
    os.makedirs(archive_dir, exist_ok=True)
    return os.path.join(archive_dir, f"fo_bhav_{fmt_yyyymmdd(trade_date)}.zip")


def _extract_csv_text(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("F&O bhav zip contains no CSV file")
        with zf.open(names[0]) as fh:
            return fh.read().decode("utf-8", errors="replace")


def _parse_rows(csv_text: str, trade_date: date) -> List[FoBhavRow]:
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        raise ValueError("Empty F&O bhav CSV")
    headers = {h.strip() for h in reader.fieldnames}
    missing = [c for c in _REQUIRED_COLS if c not in headers]
    if missing:
        raise ValueError(f"F&O bhav CSV missing required columns: {missing}")

    # NSE bhav-copy instrument codes:
    #   Legacy (pre-2024): OPTIDX (index option), OPTSTK (stock option)
    #   Current (2024+):   IDO    (index option), STO    (stock option)
    # Map both forms to our canonical OPTIDX / OPTSTK so downstream code
    # (FoBhavRow, repos, engine) keeps using the legacy values.
    _INSTR_MAP = {"OPTIDX": "OPTIDX", "OPTSTK": "OPTSTK",
                  "IDO": "OPTIDX",    "STO": "OPTSTK"}

    out: List[FoBhavRow] = []
    bad = 0
    for raw in reader:
        try:
            instr_raw = (raw.get("FinInstrmTp") or "").strip().upper()
            instr = _INSTR_MAP.get(instr_raw)
            if instr is None:
                continue
            opt_type = (raw.get("OptnTp") or "").strip().upper()
            if opt_type not in ("CE", "PE"):
                continue
            symbol = (raw.get("TckrSymb") or "").strip().upper()
            if not symbol:
                continue
            strike = safe_float(raw.get("StrkPric"))
            if strike is None:
                continue
            expiry = parse_nse_expiry(raw.get("XpryDt") or "")
            row = FoBhavRow(
                trade_date    = trade_date,
                symbol        = symbol,
                instrument    = instr,
                expiry_date   = expiry,
                strike        = strike,
                option_type   = opt_type,
                open_price    = safe_float(raw.get("OpnPric"), 0.0) or 0.0,
                high_price    = safe_float(raw.get("HghPric"), 0.0) or 0.0,
                low_price     = safe_float(raw.get("LwPric"), 0.0) or 0.0,
                close_price   = safe_float(raw.get("ClsPric"), 0.0) or 0.0,
                settle_price  = safe_float(raw.get("SttlmPric"), 0.0) or 0.0,
                contracts     = safe_int(raw.get("TtlTradgVol"), 0) or 0,
                open_interest = safe_int(raw.get("OpnIntrst"), 0) or 0,
                change_in_oi  = safe_int(raw.get("ChngInOpnIntrst"), 0) or 0,
            )
            out.append(row)
        except Exception as exc:
            bad += 1
            if bad <= 5:
                logger.warning("Skipping bad F&O row: %s — %s", exc, raw)
    if bad:
        logger.warning("F&O bhav: skipped %d malformed rows", bad)
    return out


def download_fo_bhav(trade_date: date, *, archive: bool = True) -> List[FoBhavRow]:
    """Download + parse F&O bhav copy for `trade_date`. Returns empty list on
    404 (e.g., holiday). Raises on transport / format errors."""
    url = _build_url(trade_date)
    logger.info("Downloading F&O bhav %s: %s", trade_date, url)
    session = make_session()
    resp = fetch_with_retry(session, url, accept_404=True)
    if resp is None:
        logger.warning("F&O bhav not available for %s (404 — possibly holiday)", trade_date)
        return []
    zip_bytes = resp.content

    if archive:
        try:
            with open(_archive_path(trade_date), "wb") as fh:
                fh.write(zip_bytes)
        except OSError as exc:
            logger.warning("Could not archive F&O bhav zip: %s", exc)

    csv_text = _extract_csv_text(zip_bytes)
    rows = _parse_rows(csv_text, trade_date)
    logger.info("F&O bhav %s parsed: %d option rows", trade_date, len(rows))
    return rows


def extract_index_spots(trade_date: date, symbols: List[str]) -> dict[str, float]:
    """Return {symbol: spot_close} for index underlyings on `trade_date`,
    derived from the F&O bhav `UndrlygPric` column. Empty dict if bhav
    unavailable. Reads the archived zip if present (avoids a re-download)."""
    archived = _archive_path(trade_date)
    zip_bytes: Optional[bytes] = None
    if os.path.isfile(archived):
        try:
            with open(archived, "rb") as fh:
                zip_bytes = fh.read()
        except OSError:
            zip_bytes = None
    if zip_bytes is None:
        url = _build_url(trade_date)
        session = make_session()
        resp = fetch_with_retry(session, url, accept_404=True)
        if resp is None:
            return {}
        zip_bytes = resp.content
    csv_text = _extract_csv_text(zip_bytes)
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return {}
    target = {s.upper() for s in symbols}
    out: dict[str, float] = {}
    for raw in reader:
        sym = (raw.get("TckrSymb") or "").strip().upper()
        if sym not in target or sym in out:
            continue
        instr = (raw.get("FinInstrmTp") or "").strip().upper()
        if instr not in ("IDO", "OPTIDX", "IDF", "FUTIDX"):
            continue
        spot = safe_float(raw.get("UndrlygPric"))
        if spot and spot > 0:
            out[sym] = spot
        if len(out) == len(target):
            break
    return out
