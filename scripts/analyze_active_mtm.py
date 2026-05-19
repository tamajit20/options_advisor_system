"""Compute approximate current MTM for ACTIVE trades using latest available option premiums."""
from __future__ import annotations

from database.connection import SQLServerConnection


def fmt(n):
    if n is None:
        return "n/a"
    try:
        return f"{float(n):,.0f}"
    except Exception:
        return str(n)


def last_premium(db, symbol, expiry, strike, opt):
    """Return latest close premium for an option contract."""
    row = db.fetch_one(
        "SELECT TOP 1 close_price, trade_date FROM options_fo_eod "
        "WHERE symbol=? AND expiry_date=? AND strike=? AND option_type=? "
        "ORDER BY trade_date DESC",
        [symbol, expiry, strike, opt],
    )
    return row


def main():
    db = SQLServerConnection()

    active = db.fetch_all(
        "SELECT t.trade_id, t.executed_on, t.net_credit_actual, t.actual_max_profit, "
        "t.actual_max_loss, s.strategy, s.strategy_type, s.underlying, s.expiry_date, s.dte "
        "FROM options_trades t LEFT JOIN options_suggestions s ON s.suggestion_id=t.suggestion_id "
        "WHERE t.status='ACTIVE' ORDER BY t.executed_on"
    )

    total_unreal = 0.0
    print(f"{'trade_id':<22} {'sym':<10} {'strat':<16} {'credit':>9} {'maxL':>9} {'cur_mtm':>10}")
    for t in active:
        legs = db.fetch_all(
            "SELECT tl.leg_order, tl.fill_price, tl.lots_actual, "
            "sl.action, sl.option_type, sl.strike, sl.lot_size, sl.symbol, sl.expiry_date "
            "FROM options_trade_legs tl "
            "JOIN options_suggestion_legs sl ON sl.id=tl.suggestion_leg_id "
            "WHERE tl.trade_id=? ORDER BY tl.leg_order",
            [t["trade_id"]],
        )
        mtm = 0.0
        last_date = None
        valid = True
        for L in legs:
            row = last_premium(db, L["symbol"], L["expiry_date"], L["strike"], L["option_type"])
            if not row:
                valid = False
                break
            cur = float(row["close_price"])
            last_date = row["trade_date"]
            fill = float(L["fill_price"] or 0)
            lots = int(L["lots_actual"] or 1)
            lot_size = int(L["lot_size"] or 1)
            qty = lots * lot_size
            sign = -1 if (L.get("action") or "").upper() == "SELL" else +1
            leg_mtm = sign * (cur - fill) * qty
            mtm += leg_mtm
        if valid:
            total_unreal += mtm
            mtm_str = fmt(mtm)
        else:
            mtm_str = "no_data"
        date_str = str(last_date) if last_date else ""
        print(
            f"{t['trade_id']:<22} {t.get('underlying',''):<10} {t.get('strategy','')[:16]:<16} "
            f"{fmt(t['net_credit_actual']):>9} {fmt(t['actual_max_loss']):>9} {mtm_str:>10}  as_of={date_str}"
        )

    print(f"\nTotal unrealized MTM across active trades: Rs.{fmt(total_unreal)}")

    # Also closed PnL
    closed_pnl = db.fetch_one(
        "SELECT SUM(net_pnl) AS n FROM options_trades WHERE status IN ('CLOSED','EXPIRED')"
    )
    print(f"Total realised P&L (closed trades):         Rs.{fmt(closed_pnl['n'])}")
    print(f"Combined Rs.{fmt(total_unreal + float(closed_pnl['n'] or 0))}")

    db.close()


if __name__ == "__main__":
    main()
