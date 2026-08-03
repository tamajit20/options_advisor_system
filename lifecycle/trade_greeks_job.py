"""
lifecycle/trade_greeks_job.py
=============================

C6 — Daily Greek drift tracking for open trades.

Runs after EOD bhav data arrives (requires fo_bhav + spot_bhav).
For each open trade, fetches the current spot, looks up each leg's ATM IV
from iv_history, and writes delta/gamma/vega/theta to options_trade_greeks.

Output:
    options_trade_greeks (trade_id, as_of_date, net_delta, net_gamma,
                          net_vega, net_theta, legs_json)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from database.connection import SQLServerConnection
from database.models import (
    IvHistoryRepo,
    SpotEodRepo,
    TradeGreeksRepo,
    TradeRepo,
)
from engine.trade_greeks import compute_trade_greeks
from utils import today_ist
from lifecycle.eod_session import effective_bhav_end_date

logger = logging.getLogger(__name__)


def run_trade_greeks_update(
    db: SQLServerConnection,
    trade_date: Optional[date] = None,
) -> int:
    """Recompute and persist delta/vega/theta for all open trades.

    Returns the number of trades processed.
    """
    trade_date = trade_date or effective_bhav_end_date()
    trd  = TradeRepo(db)
    sp   = SpotEodRepo(db)
    iv   = IvHistoryRepo(db)
    grk  = TradeGreeksRepo(db)
    grk.ensure_table()

    open_trades = trd.open_trades()
    updated = 0

    for trade in open_trades:
        trade_id   = trade["trade_id"]
        sug_id     = trade.get("suggestion_id")
        if not sug_id:
            continue

        sug_legs = db.fetch_all(
            "SELECT * FROM options_suggestion_legs "
            "WHERE suggestion_id = ? ORDER BY leg_order",
            [sug_id],
        )
        if not sug_legs:
            continue

        underlying = sug_legs[0]["symbol"]
        expiry     = sug_legs[0]["expiry_date"]

        spot_row = sp.for_date(underlying, trade_date)
        if not spot_row:
            logger.debug("trade_greeks: no spot for %s on %s — skip", underlying, trade_date)
            continue
        spot = float(spot_row["close_price"])

        iv_rows = iv.latest_for(underlying, trade_date)
        atm_iv_for_expiry = None
        for r in iv_rows:
            if r.get("expiry_date") == expiry:
                atm_iv_for_expiry = float(r.get("atm_iv") or 0.0) or None
                break
        if atm_iv_for_expiry is None and iv_rows:
            atm_iv_for_expiry = float(iv_rows[0].get("atm_iv") or 0.20) or 0.20

        # Enrich each suggestion leg with the IV we found
        enriched_legs = [
            {**leg, "atm_iv": atm_iv_for_expiry or 0.20}
            for leg in sug_legs
        ]

        try:
            result = compute_trade_greeks(
                enriched_legs,
                spot=spot,
                trade_date=trade_date,
            )
            grk.upsert(trade_id, trade_date, result)
            updated += 1
        except Exception:
            logger.exception("trade_greeks: failed for trade %s", trade_id)

    db.commit()
    logger.info("Greek drift update: %d trades processed for %s", updated, trade_date)
    return updated
