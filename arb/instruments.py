"""
arb/instruments.py — build NSE↔BSE dual-listed pairs from Kite instrument master.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set

from config import ARB_CONFIG, NIFTY_50_SYMBOLS
from providers.zerodha.instruments import Instrument, InstrumentMaster

logger = logging.getLogger(__name__)


def _eq_by_exchange(master: InstrumentMaster) -> tuple[Dict[str, Instrument], Dict[str, Instrument]]:
    """Return NSE and BSE EQ instruments keyed by tradingsymbol (upper)."""
    master.refresh_if_stale()
    nse = {i.tradingsymbol.upper(): i for i in master.list_nse_equity()}
    bse = {i.tradingsymbol.upper(): i for i in master.list_bse_equity()}
    return nse, bse


def _isin_map(instruments: Dict[str, Instrument]) -> Dict[str, Instrument]:
    out: Dict[str, Instrument] = {}
    for inst in instruments.values():
        isin = (inst.isin or "").strip().upper()
        if isin:
            out[isin] = inst
    return out


def build_dual_listed_pairs(
    master: InstrumentMaster,
    *,
    universe: Optional[str] = None,
) -> List[dict]:
    """Match NSE EQ ∩ BSE EQ by ISIN when available, else tradingsymbol."""
    universe = (universe or ARB_CONFIG.get("universe") or "nifty50_dual").lower()
    nse, bse = _eq_by_exchange(master)
    nse_by_isin = _isin_map(nse)
    bse_by_isin = _isin_map(bse)

    matched: List[dict] = []
    seen: Set[str] = set()

    for isin, n_inst in nse_by_isin.items():
        b_inst = bse_by_isin.get(isin)
        if b_inst is None:
            continue
        sym = n_inst.tradingsymbol.upper()
        if sym in seen:
            continue
        seen.add(sym)
        matched.append(_pair_row(sym, n_inst, b_inst, isin))

    for sym, n_inst in nse.items():
        if sym in seen:
            continue
        b_inst = bse.get(sym)
        if b_inst is None:
            continue
        seen.add(sym)
        matched.append(_pair_row(sym, n_inst, b_inst, n_inst.isin or b_inst.isin))

    if universe == "nifty50_dual":
        allow = {s.upper() for s in NIFTY_50_SYMBOLS}
        matched = [p for p in matched if p["symbol"] in allow]

    matched.sort(key=lambda p: p["symbol"])
    logger.info("arb instruments: built %d dual-listed pairs (universe=%s)", len(matched), universe)
    return matched


def _pair_row(sym: str, n_inst: Instrument, b_inst: Instrument, isin: Optional[str]) -> dict:
    isin_clean = (isin or "").strip().upper() or None
    return {
        "symbol": sym,
        "nse_symbol": n_inst.tradingsymbol.upper(),
        "bse_symbol": b_inst.tradingsymbol.upper(),
        "isin": isin_clean,
        "nse_token": int(n_inst.instrument_token),
        "bse_token": int(b_inst.instrument_token),
    }


def refresh_pairs_to_db(db, master: InstrumentMaster, *, universe: Optional[str] = None) -> int:
    """Rebuild arb_pairs from instrument master. Returns pair count."""
    from database.arb_models import ArbPairRepo

    pairs = build_dual_listed_pairs(master, universe=universe)
    repo = ArbPairRepo(db)
    symbols = []
    for p in pairs:
        repo.upsert_pair(**p)
        symbols.append(p["symbol"])
    repo.deactivate_missing(symbols)
    return len(pairs)
