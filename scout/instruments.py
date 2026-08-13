"""NSE equity universe from Zerodha Kite instrument master (includes new IPOs)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import List, Optional, Tuple

from config import NIFTY_50_SYMBOLS, NIFTY_BANK_SYMBOLS, ZERODHA_API_CONFIG
from providers.zerodha.facade import KiteFacade
from providers.zerodha.instruments import InstrumentMaster
from providers.zerodha.session import is_token_valid, load_session
from scout.index_groups import index_tags, sort_watchlist_rows
from utils import now_ist

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_master: Optional[InstrumentMaster] = None
_master_token: Optional[str] = None

_NIFTY50_SET = frozenset(s.upper() for s in NIFTY_50_SYMBOLS)


def _equity_display_name(inst: Instrument) -> str:
    """Company name for UI — omit blank, numeric, or symbol-dupe Kite values."""
    name = (inst.name or "").strip()
    sym = inst.tradingsymbol.upper()
    if not name:
        return ""
    if name.isdigit():
        return ""
    if name.upper() == sym:
        return ""
    return name


def _stock_row(inst: Instrument) -> dict:
    sym = inst.tradingsymbol.upper()
    return {
        "symbol": sym,
        "name": _equity_display_name(inst),
        "is_nifty50": sym in _NIFTY50_SET,
        "index_tags": index_tags(inst.tradingsymbol),
    }


class ScoutInstrumentError(Exception):
    pass


def _session_master(force_refresh: bool = False) -> InstrumentMaster:
    global _master, _master_token
    sess = load_session()
    if not sess or not sess.access_token or not is_token_valid(sess):
        raise ScoutInstrumentError(
            "Zerodha login required — use the 🔑 button (same as Options Advisor)"
        )
    token = sess.access_token
    with _lock:
        if _master is None or _master_token != token:
            facade = KiteFacade(
                api_key=ZERODHA_API_CONFIG["api_key"],
                access_token=token,
            )
            _master = InstrumentMaster(loader=lambda: facade.instruments("NSE"))
            _master_token = token
            force_refresh = True
        master = _master
    if force_refresh:
        master.refresh()
    else:
        master.refresh_if_stale()
    return master


def refresh_nse_equity_master() -> int:
    """Force reload from Kite (picks up IPO listings after Zerodha updates master)."""
    return _session_master(force_refresh=True).refresh()


def nse_equity_universe(
    *,
    search: str = "",
    offset: int = 0,
    limit: int = 80,
    force_refresh: bool = False,
) -> Tuple[List[dict], int, Optional[str]]:
    """Return (page of stock dicts, total_count, refreshed_at_iso)."""
    master = _session_master(force_refresh=force_refresh)
    rows = master.list_nse_equity()
    q = (search or "").strip().upper()
    if q:
        filtered = [
            inst for inst in rows
            if q in inst.tradingsymbol.upper() or q in (inst.name or "").upper()
        ]
    else:
        filtered = rows
    total = len(filtered)
    stock_rows = [_stock_row(inst) for inst in filtered]
    if not search:
        stock_rows = sort_watchlist_rows(stock_rows)
    page = stock_rows[offset: offset + limit]
    stocks = page
    loaded = master.loaded_at_monotonic
    if loaded is not None:
        refreshed = (
            now_ist() - timedelta(seconds=time.monotonic() - loaded)
        ).isoformat(sep=" ", timespec="seconds")
    else:
        refreshed = None
    return stocks, total, refreshed


def nifty50_symbols() -> List[str]:
    return list(NIFTY_50_SYMBOLS)


def equity_rows_for_symbols(symbols: List[str]) -> List[dict]:
    """Lookup NSE EQ rows (with names) for saved/selected symbols."""
    master = _session_master()
    rows: List[dict] = []
    for raw in symbols:
        sym = str(raw or "").upper().strip()
        if not sym:
            continue
        inst = master.get_by_tradingsymbol("NSE", sym)
        if inst is None or inst.instrument_type != "EQ":
            continue
        rows.append(_stock_row(inst))
    return sort_watchlist_rows(rows)


def valid_nse_symbols(symbols: List[str], *, force_refresh: bool = False) -> List[str]:
    """Keep only symbols present in the current instrument master."""
    master = _session_master(force_refresh=force_refresh)
    valid = {
        inst.tradingsymbol.upper()
        for inst in master.list_nse_equity()
    }
    return sorted(s for s in {str(x).upper().strip() for x in symbols if x} if s in valid)
