"""
lifecycle/exit_orchestrator.py
==============================

Daily exit-decision orchestrator. For each open trade:
    1. Load trade + legs
    2. Get current chain mid prices
    3. Run engine.exit_engine.evaluate_exit
    4. Update trade.daily_status + exit_instruction
    5. On TAKE_PROFIT / SL_HIT / EXPIRE, mark closed (P&L computed at close)
    6. Emit notification on non-HOLD decisions
"""

from __future__ import annotations

import logging
from datetime import date

from contracts import Notification
from database.connection import SQLServerConnection
from database.models import FoEodRepo, NotificationRepo, TradeRepo
from engine.exit_engine import evaluate_exit
from utils import days_between, now_ist, today_ist

logger = logging.getLogger(__name__)


def _close_trade_with_charges(db: SQLServerConnection, trade_id: str,
                              gross_pnl: float) -> None:
    """Close the trade — for now we use the existing P&L and assume charges
    were captured at suggestion time (re-using suggestion estimate)."""
    trd = TradeRepo(db)
    trade = trd.get(trade_id)
    if trade is None:
        return
    charges = float(trade.get("total_charges") or 0.0)
    net = gross_pnl - charges
    trd.close_trade(trade_id, gross=gross_pnl, charges=charges, net=net)


def run_exit_engine(db: SQLServerConnection, trade_date: date | None = None) -> int:
    trade_date = trade_date or today_ist()
    trd = TradeRepo(db)
    fo = FoEodRepo(db)
    notif = NotificationRepo(db)

    open_trades = trd.open_trades()
    decisions_made = 0

    for trade in open_trades:
        trade_id = trade["trade_id"]
        legs = trd.legs(trade_id)
        if not legs:
            continue
        # All legs share underlying + expiry (they're from one suggestion)
        # We need to look up the suggestion legs to get strike/option_type
        # But trade_legs only has fill prices. Pull suggestion legs:
        sug_legs = db.fetch_all(
            "SELECT * FROM options_suggestion_legs WHERE suggestion_id = ? ORDER BY leg_order",
            [trade["suggestion_id"]],
        )
        if not sug_legs:
            continue

        underlying = sug_legs[0]["symbol"]
        expiry = sug_legs[0]["expiry_date"]
        dte = days_between(trade_date, expiry)

        # Current chain — skip this trade if no EOD data for today (holiday/weekend).
        # Without chain data, all mid_prices would be 0 which causes evaluate_exit
        # to see full profit on every credit leg and fire spurious TAKE_PROFIT signals.
        chain_rows = fo.get_chain(underlying, trade_date, expiry)
        if not chain_rows:
            logger.info(
                "Exit engine: no chain data for %s/%s on %s — skipping (holiday/weekend)",
                underlying, expiry, trade_date,
            )
            continue
        current_chain = [
            {
                "strike":     float(c["strike"]),
                "option_type": c["option_type"],
                "mid_price":  float(c.get("settle_price") or c.get("close_price") or 0.0),
            }
            for c in chain_rows
        ]

        legs_for_engine = []
        by_order = {l["leg_order"]: l for l in legs}
        for sl in sug_legs:
            tl = by_order.get(sl["leg_order"])
            if not tl or not tl.get("executed"):
                continue
            legs_for_engine.append({
                "action":      sl["action"],
                "strike":      float(sl["strike"]),
                "option_type": sl["option_type"],
                "lots":        sl["lots"],
                "lot_size":    sl["lot_size"],
                "fill_price":  tl.get("fill_price"),
            })

        if not legs_for_engine:
            continue

        decision = evaluate_exit(
            trade_id=trade_id,
            legs=legs_for_engine,
            current_chain=current_chain,
            entry_net_credit=float(trade.get("net_credit_actual") or 0.0),
            max_profit_rs=float(trade.get("actual_max_profit") or 0.0),
            max_loss_rs=float(trade.get("actual_max_loss") or 0.0),
            sl_level_per_share=trade.get("actual_stop_loss_level"),
            days_to_expiry=dte,
            as_of=now_ist(),
        )
        decisions_made += 1

        # Update trade — never auto-close. Always wait for the user to record
        # actual broker exit fills via the Close Trade UI. We surface a clear
        # daily_status, an exit instruction containing suggested per-leg
        # closing prices, and notify so the user can act.
        if decision.decision == "HOLD":
            trd.update_status(trade_id, "ACTIVE", "OPEN", None)
        else:
            # Build per-leg suggested closing prices (mid of latest chain)
            suggested_lines = []
            est_gross = 0.0
            for leg in legs_for_engine:
                key = (leg["strike"], leg["option_type"])
                mid = next((c["mid_price"] for c in current_chain
                            if (c["strike"], c["option_type"]) == key), 0.0)
                qty = int(leg["lots"]) * int(leg["lot_size"])
                fill = float(leg.get("fill_price") or 0.0)
                close_action = "Buy back" if leg["action"] == "SELL" else "Sell back"
                suggested_lines.append(
                    f"{close_action} {leg['strike']:g} {leg['option_type']} @ ~₹{mid:.2f}"
                )
                if leg["action"] == "SELL":
                    est_gross += (fill - mid) * qty
                else:
                    est_gross += (mid - fill) * qty
            instruction = (
                f"{decision.reason} | Suggested close: "
                + "; ".join(suggested_lines)
                + f" | Est. P&L ₹{est_gross:.0f}"
                + " | Record actual fills via 'Close Trade'."
            )
            daily = "EXIT_AT_OPEN" if decision.decision == "EXIT_TOMORROW" else decision.decision
            trd.update_status(trade_id, "ACTIVE", daily, instruction)

            # Notification
            notif.insert(Notification(
                created_at=now_ist(),
                notif_type=f"EXIT_{decision.decision}",
                severity="WARNING" if decision.decision in ("SL_HIT",) else "INFO",
                title=f"{trade.get('trade_name') or trade_id}: {decision.decision} — close pending",
                body=instruction,
                related_trade_id=trade_id,
            ))

    db.commit()
    logger.info("Exit engine: %d open trades evaluated", decisions_made)
    return decisions_made
