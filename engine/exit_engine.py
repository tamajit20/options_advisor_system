"""
engine/exit_engine.py
=====================

Daily exit-decision engine. Pure function.

Inputs:
    - trade snapshot: legs (with action/strike/option_type), entry_credit, max_profit
    - current chain: list of dicts (mid prices for each leg's strike/type)
    - days_to_expiry
    - indicators (for SL_HIT detection: SL hit if trade is X% in loss)

Decision codes:
    HOLD          — keep
    EXIT_TOMORROW — DTE ≤ 1, close at next open
    SL_HIT        — current loss ≥ SL level
    EXPIRE        — DTE = 0
    TAKE_PROFIT   — current profit ≥ take_profit_fraction × max_profit
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Mapping, Sequence

from config import STRATEGY_CONFIG
from contracts import ExitDecision


def evaluate_exit(
    *,
    trade_id: str,
    legs: Sequence[Mapping],         # each: action, strike, option_type, fill_price, lots, lot_size
    current_chain: Sequence[Mapping],  # each: strike, option_type, mid_price
    entry_net_credit: float,         # in rupees, signed
    max_profit_rs: float,
    max_loss_rs: float,
    sl_level_per_share: float | None,
    days_to_expiry: int,
    as_of: datetime | None = None,
) -> ExitDecision:
    as_of = as_of or datetime.utcnow()

    if days_to_expiry == 0:
        return ExitDecision(trade_id=trade_id, decision="EXPIRE",
                            reason="DTE=0 — settle today", as_of=as_of)

    # Compute current MTM
    chain_lookup: dict[tuple[float, str], float] = {
        (float(r["strike"]), r["option_type"]): float(r.get("mid_price") or 0.0)
        for r in current_chain
    }

    current_value = 0.0  # what it costs to close the position now
    qty_total = 0
    for leg in legs:
        key = (float(leg["strike"]), leg["option_type"])
        mid = chain_lookup.get(key, 0.0)
        lots = int(leg.get("lots") or 0)
        lot_size = int(leg.get("lot_size") or 0)
        qty = lots * lot_size
        qty_total += qty
        # We OPENED the leg with `action`; closing flips the sign.
        # If we sold, closing = buy back (we PAY mid). If we bought, closing = sell (we RECEIVE mid).
        sign = -1.0 if leg["action"] == "SELL" else 1.0
        current_value += sign * mid * qty

    # current_pnl (rupees) = entry_net_credit + current_value
    # entry_net_credit is positive for credit strategies; current_value is what we'd net if we closed.
    current_pnl = entry_net_credit + current_value

    # Take profit
    tp_fraction = STRATEGY_CONFIG["take_profit_fraction"]
    if max_profit_rs > 0 and current_pnl >= tp_fraction * max_profit_rs:
        return ExitDecision(
            trade_id=trade_id, decision="TAKE_PROFIT",
            reason=f"Captured ≥{tp_fraction*100:.0f}% of max profit "
                   f"(₹{current_pnl:.0f} of ₹{max_profit_rs:.0f})",
            as_of=as_of,
        )

    # SL hit (per-share level applied via average lot qty)
    if sl_level_per_share is not None and qty_total > 0:
        sl_rs = -sl_level_per_share * (qty_total // max(len(legs), 1))
        if current_pnl <= sl_rs:
            return ExitDecision(
                trade_id=trade_id, decision="SL_HIT",
                reason=f"Loss ≥ SL: ₹{current_pnl:.0f} ≤ ₹{sl_rs:.0f}",
                as_of=as_of,
            )

    if days_to_expiry <= 1:
        return ExitDecision(
            trade_id=trade_id, decision="EXIT_TOMORROW",
            reason=f"DTE={days_to_expiry} — close at next open to avoid expiry risk",
            as_of=as_of,
        )

    return ExitDecision(
        trade_id=trade_id, decision="HOLD",
        reason=f"In-band: P&L ₹{current_pnl:.0f}, DTE {days_to_expiry}",
        as_of=as_of,
    )
