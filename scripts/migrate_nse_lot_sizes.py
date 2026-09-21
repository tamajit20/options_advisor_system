"""Align lot sizes to NSE Jan-2026 revision and recalc closed-trade P&L.

NSE revised index F&O lots for contracts from Jan 2026 onward:
  NIFTY 75→65, BANKNIFTY 35→30, FINNIFTY 65→60.

Updates:
  1. options_lot_sizes (engine reads this before config defaults)
  2. options_suggestion_legs.lot_size stamps still on old values
  3. CLOSED trade leg_pnl / gross_pnl / charges / net_pnl from fill+exit

Idempotent: re-running recomputes P&L from prices at the new lot size.
"""

from __future__ import annotations

from datetime import date

from database.connection import SQLServerConnection
from database.models import LotSizeRepo
from engine.charges import estimate_charges_per_txn

NEW_LOTS = {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60}
OLD_LOTS = {"NIFTY": 75, "BANKNIFTY": 35, "FINNIFTY": 65}


def migrate(db: SQLServerConnection) -> int:
    lot_repo = LotSizeRepo(db)
    for sym, ls in NEW_LOTS.items():
        lot_repo.upsert(sym, date(2026, 1, 1), ls)

    for sym, old in OLD_LOTS.items():
        db.execute(
            "UPDATE options_suggestion_legs SET lot_size=? WHERE symbol=? AND lot_size=?",
            [NEW_LOTS[sym], sym, old],
        ).close()

    trades = db.fetch_all(
        """
        SELECT DISTINCT t.trade_id
        FROM options_trades t
        JOIN options_trade_legs tl ON tl.trade_id = t.trade_id
        JOIN options_suggestion_legs sl ON sl.id = tl.suggestion_leg_id
        WHERE t.status = 'CLOSED'
          AND tl.executed = 1
          AND tl.fill_price IS NOT NULL
          AND tl.exit_price IS NOT NULL
          AND sl.symbol IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY')
        ORDER BY t.trade_id
        """
    )

    n = 0
    for tr in trades:
        tid = tr["trade_id"]
        legs = db.fetch_all(
            """
            SELECT tl.id AS leg_id, tl.fill_price, tl.exit_price, tl.lots_actual,
                   sl.action, sl.lots, sl.lot_size, sl.symbol
            FROM options_trade_legs tl
            JOIN options_suggestion_legs sl ON sl.id = tl.suggestion_leg_id
            WHERE tl.trade_id=? AND tl.executed=1
            ORDER BY tl.leg_order
            """,
            [tid],
        )
        if not legs:
            continue

        gross = 0.0
        txn_legs: list[dict] = []
        ratio = None
        for lg in legs:
            sym = lg["symbol"]
            new_ls = int(lg["lot_size"])
            old_ls = OLD_LOTS.get(sym, new_ls)
            if ratio is None and old_ls and new_ls != old_ls:
                ratio = new_ls / float(old_ls)

            lots = int(lg["lots_actual"] or lg["lots"] or 0)
            fill = float(lg["fill_price"])
            exit_p = float(lg["exit_price"])
            sign = 1.0 if lg["action"] == "SELL" else -1.0
            leg_pnl = sign * (fill - exit_p) * lots * new_ls
            gross += leg_pnl
            db.execute(
                "UPDATE options_trade_legs SET leg_pnl=? WHERE id=?",
                [round(leg_pnl, 4), lg["leg_id"]],
            ).close()

            txn_legs.append(
                {"action": lg["action"], "price": fill, "lots": lots, "lot_size": new_ls}
            )
            close_action = "BUY" if lg["action"] == "SELL" else "SELL"
            txn_legs.append(
                {"action": close_action, "price": exit_p, "lots": lots, "lot_size": new_ls}
            )

        charges = estimate_charges_per_txn(txn_legs).total if txn_legs else 0.0
        net = round(gross - charges, 4)

        trow = db.fetch_one(
            "SELECT net_credit_actual, actual_max_profit, actual_max_loss "
            "FROM options_trades WHERE trade_id=?",
            [tid],
        )
        updates: dict = {
            "gross_pnl": round(gross, 4),
            "total_charges": round(charges, 4),
            "net_pnl": net,
        }
        if ratio and ratio != 1.0 and trow:
            for col in ("net_credit_actual", "actual_max_profit", "actual_max_loss"):
                val = trow.get(col)
                if val is not None:
                    updates[col] = round(float(val) * ratio, 4)

        sets = ", ".join(f"{k}=?" for k in updates)
        db.execute(
            f"UPDATE options_trades SET {sets} WHERE trade_id=?",
            [*updates.values(), tid],
        ).close()
        n += 1

    db.commit()
    return n


def main() -> None:
    db = SQLServerConnection()
    try:
        n = migrate(db)
        print(f"closed_trades_recalculated={n}")
        for sym, ls in NEW_LOTS.items():
            print(f"  {sym} lot_size={ls}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
