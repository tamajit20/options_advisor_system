"""
lifecycle/iv_orchestrator.py
============================

For each (symbol, expiry, strike, option_type) on the latest trade date:
    1. Fetch market price from F&O EOD
    2. Fetch spot from spot EOD
    3. Compute IV (Black-Scholes bisection)
    4. Compute ATM IV per (symbol, expiry)
    5. Compute IV Rank (52w window) per (symbol, expiry, atm)
    6. Upsert into options_iv_history
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List

from config import STRATEGY_CONFIG
from database.connection import SQLServerConnection
from database.models import FoEodRepo, IvHistoryRepo, SpotEodRepo
from engine.iv_calculator import implied_vol
from engine.iv_rank import iv_percentile, iv_rank as compute_iv_rank, pick_atm_iv
from utils import days_between, today_ist

logger = logging.getLogger(__name__)


def run_iv_calculation(
    db: SQLServerConnection,
    trade_date: date | None = None,
) -> int:
    trade_date = trade_date or today_ist()
    fo = FoEodRepo(db)
    sp = SpotEodRepo(db)
    iv_repo = IvHistoryRepo(db)

    # If no fo data for trade_date, try the latest available
    if fo.latest_trade_date() != trade_date:
        latest = fo.latest_trade_date()
        if latest is None:
            logger.warning("No FO data — skipping IV calc")
            return 0
        trade_date = latest

    total_rows = 0
    underlyings: List[str] = STRATEGY_CONFIG["underlyings"]

    for symbol in underlyings:
        spot_row = sp.latest(symbol)
        if not spot_row or float(spot_row["close_price"]) <= 0:
            logger.warning("IV: no spot for %s on %s", symbol, trade_date)
            continue
        spot = float(spot_row["close_price"])

        expiries = fo.expiries_for(symbol, trade_date)
        if not expiries:
            continue

        for expiry in expiries:
            dte = days_between(trade_date, expiry)
            if dte <= 0:
                continue

            chain = fo.get_chain(symbol, trade_date, expiry)
            if not chain:
                continue

            # Compute IV for every option
            rows: List[Dict] = []
            triplets: List[tuple[float, str, float]] = []
            for r in chain:
                strike = float(r["strike"])
                opt_type = r["option_type"]
                market_price = float(r.get("settle_price") or r.get("close_price") or 0.0)
                if market_price <= 0:
                    continue
                iv, converged = implied_vol(
                    market_price=market_price,
                    spot=spot,
                    strike=strike,
                    days_to_expiry=dte,
                    option_type=opt_type,
                )
                if iv <= 0:
                    continue
                triplets.append((strike, opt_type, iv))
                rows.append({
                    "trade_date":   trade_date,
                    "symbol":       symbol,
                    "expiry_date":  expiry,
                    "strike":       strike,
                    "option_type":  opt_type,
                    "spot":         spot,
                    "market_price": market_price,
                    "iv":           iv,
                    "converged":    converged,
                    "atm_iv":       None,    # filled below
                    "iv_rank":      None,
                    "iv_percentile": None,
                })

            if not rows:
                continue

            # ATM IV for this expiry
            atm_iv = pick_atm_iv(triplets, spot)

            # IV Rank: needs 52w history of ATM IV for this expiry
            since = trade_date - timedelta(days=365)
            history_rows = iv_repo.atm_iv_history(symbol, since)
            history_values = [float(h["atm_iv"]) for h in history_rows
                              if h.get("atm_iv") is not None]

            ivr = compute_iv_rank(atm_iv or 0.0, history_values) if atm_iv else 0.0
            ivp = iv_percentile(atm_iv or 0.0, history_values) if atm_iv else 0.0

            # Stamp atm_iv / iv_rank / iv_percentile on every row for this expiry
            for r in rows:
                r["atm_iv"] = atm_iv
                r["iv_rank"] = ivr
                r["iv_percentile"] = ivp

            n = iv_repo.upsert_many(rows)
            total_rows += n
            logger.info("IV: %s exp=%s rows=%d ATM_IV=%.4f IVR=%.1f",
                        symbol, expiry, n, atm_iv or 0.0, ivr)

    db.commit()
    logger.info("IV calc total rows: %d", total_rows)
    return total_rows
