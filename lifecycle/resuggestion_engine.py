"""
lifecycle/resuggestion_engine.py
================================

Generates a re-suggestion for a broken trade. At MOST one re-suggestion per
original suggestion — enforced by UNIQUE constraint on
options_resuggestions.original_suggestion_id.

The "revised" suggestion is the cheapest path to either:
    a) Complete the structure (add the missing legs at current chain prices)
    b) Convert into a defined-risk variant
"""

from __future__ import annotations

import json
import logging
from datetime import date

from database.connection import SQLServerConnection
from database.models import (
    FoEodRepo,
    ResuggestionRepo,
    SuggestionRepo,
    TradeRepo,
)
from engine.broken_trade_advisor import advise, diagnose
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)


def generate_resuggestion(
    db: SQLServerConnection,
    trade_id: str,
    trade_date: date | None = None,
) -> bool:
    """Returns True if a resuggestion row was inserted, False if skipped."""
    trade_date = trade_date or today_ist()
    trade = TradeRepo(db).get(trade_id)
    if trade is None:
        raise ValueError(f"Unknown trade: {trade_id}")
    suggestion_id = trade["suggestion_id"]

    # Already resuggested?
    re_repo = ResuggestionRepo(db)
    if re_repo.for_suggestion(suggestion_id):
        logger.info("Resuggestion already exists for %s — skipping", suggestion_id)
        return False

    # Need original legs and trade legs
    sug_repo = SuggestionRepo(db)
    sug_legs = sug_repo.legs(suggestion_id)
    trade_legs = TradeRepo(db).legs(trade_id)
    by_order = {tl["leg_order"]: tl for tl in trade_legs}

    executed = []
    not_executed = []
    for leg in sug_legs:
        tl = by_order.get(leg["leg_order"])
        if tl and tl.get("executed"):
            executed.append({**leg, "fill_price": tl.get("fill_price")})
        else:
            not_executed.append(leg)

    if not not_executed:
        return False

    # Latest chain on trade_date for re-pricing
    fo = FoEodRepo(db)
    underlying = sug_legs[0]["symbol"]
    expiry = sug_legs[0]["expiry_date"]
    chain = fo.get_chain(underlying, trade_date, expiry)
    chain_priced = [
        {**c, "mid_price": (c.get("settle_price") or c.get("close_price") or 0.0)}
        for c in chain
    ]

    state = diagnose(executed, not_executed)
    options = advise(
        state=state,
        executed_legs=executed,
        not_executed_legs=not_executed,
        spot=0.0,
        current_chain=chain_priced,
    )

    revised_legs = [
        {
            "leg_order":   leg["leg_order"],
            "symbol":      leg["symbol"],
            "expiry_date": str(leg["expiry_date"]),
            "strike":      float(leg["strike"]),
            "option_type": leg["option_type"],
            "action":      leg["action"],
            "lots":        leg["lots"],
            "lot_size":    leg["lot_size"],
            "executed":    leg in executed,
        }
        for leg in sug_legs
    ]

    combined_economics = {
        "state": state,
        "recovery_options": [
            {
                "rank": o.rank, "label": o.label, "recommended": o.recommended,
                "estimated_pnl": o.estimated_pnl, "when_to_use": o.when_to_use,
                "zerodha_steps": o.zerodha_steps, "time_sensitivity": o.time_sensitivity,
            }
            for o in options
        ],
    }

    re_repo.insert(
        original_suggestion_id=suggestion_id,
        generated_on=now_ist(),
        revised_legs=revised_legs,
        combined_economics=combined_economics,
    )
    db.commit()
    logger.info("Resuggestion inserted for %s (state=%s, %d options)",
                suggestion_id, state, len(options))
    return True
