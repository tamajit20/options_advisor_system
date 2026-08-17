"""
basis/instruments.py — build NSE EQ + near-month NFO FUT pairs from Kite master.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from config import BASIS_CONFIG, NIFTY_50_SYMBOLS
from providers.zerodha.instruments import Instrument, InstrumentMaster

logger = logging.getLogger(__name__)


def _near_month_future(master: InstrumentMaster, symbol: str, *, today: Optional[date] = None) -> Optional[Instrument]:
    """Earliest NFO FUT expiry >= today for `symbol` (Kite `name` = EQ tradingsymbol)."""
    today = today or date.today()
    futs = [
        f for f in master.list_nfo_futures(symbol)
        if f.expiry is not None and f.expiry >= today
    ]
    if not futs:
        return None
    return min(futs, key=lambda f: f.expiry)  # type: ignore[arg-type]


def build_cash_futures_pairs(
    master: InstrumentMaster,
    *,
    universe: Optional[str] = None,
    today: Optional[date] = None,
) -> List[dict]:
    """Match NSE EQ with near-month NFO FUT by tradingsymbol / name."""
    universe = (universe or BASIS_CONFIG.get("universe") or "nifty50_fo").lower()
    today = today or date.today()
    master.refresh_if_stale()

    allow: Optional[set[str]] = None
    if universe == "nifty50_fo":
        allow = {s.upper() for s in NIFTY_50_SYMBOLS}

    pairs: List[dict] = []
    for eq in master.list_nse_equity():
        sym = eq.tradingsymbol.upper()
        if allow is not None and sym not in allow:
            continue
        fut = _near_month_future(master, sym, today=today)
        if fut is None or fut.expiry is None:
            continue
        pairs.append(_pair_row(sym, eq, fut))

    pairs.sort(key=lambda p: p["symbol"])
    logger.info("basis instruments: built %d cash-futures pairs (universe=%s)", len(pairs), universe)
    return pairs


def _pair_row(sym: str, eq: Instrument, fut: Instrument) -> dict:
    return {
        "symbol": sym,
        "spot_symbol": eq.tradingsymbol.upper(),
        "fut_symbol": fut.tradingsymbol.upper(),
        "spot_token": int(eq.instrument_token),
        "fut_token": int(fut.instrument_token),
        "fut_expiry": fut.expiry,
    }


def refresh_pairs_to_db(db, master: InstrumentMaster, *, universe: Optional[str] = None) -> int:
    """Rebuild basis_pairs from instrument master. Returns pair count."""
    from database.basis_models import BasisPairRepo

    pairs = build_cash_futures_pairs(master, universe=universe)
    repo = BasisPairRepo(db)
    symbols = []
    for p in pairs:
        repo.upsert_pair(**p)
        symbols.append(p["symbol"])
    repo.deactivate_missing(symbols)
    return len(pairs)
