"""Print profit-at-close percentages for closed trades."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from repo root or mounted data/ on the VM container.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if not any(Path(p).name == "database" for p in sys.path):
    sys.path.insert(0, "/app")

from database.connection import SQLServerConnection


def leg_total_credit(db, trade_id: str) -> float:
    legs = db.fetch_all(
        "SELECT tl.lots_actual, sl.lots, sl.lot_size, sl.action, tl.fill_price "
        "FROM options_trade_legs tl "
        "JOIN options_suggestion_legs sl ON sl.id = tl.suggestion_leg_id "
        "WHERE tl.trade_id=? AND tl.executed=1",
        [trade_id],
    )
    total = 0.0
    for lg in legs:
        lots = lg["lots_actual"] or lg["lots"] or 0
        qty = lots * (lg["lot_size"] or 1)
        sign = 1 if lg["action"] == "SELL" else -1
        total += sign * float(lg["fill_price"] or 0) * qty
    return total


def main() -> None:
    db = SQLServerConnection()
    rows = db.fetch_all(
        """
        SELECT t.trade_id, t.trade_name, t.status, t.executed_on, t.closed_on,
               t.net_credit_actual, t.gross_pnl, t.net_pnl, t.total_charges,
               t.actual_max_profit, t.actual_max_loss,
               s.strategy, s.underlying, s.max_profit AS sug_max_profit,
               s.net_credit_suggested
        FROM options_trades t
        LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id
        WHERE t.status IN ('CLOSED', 'EXPIRED')
        ORDER BY COALESCE(t.closed_on, t.executed_on) DESC
        """
    )

    print("CLOSED TRADES — PROFIT AT EXIT")
    print("=" * 105)
    hdr = (
        f"{'Trade':<28} {'Strategy':<18} {'Closed':<12} "
        f"{'Gross':>9} {'Net':>9} {'MaxProf':>9} "
        f"{'%MaxProf':>9} {'%Exit':>11}"
    )
    print(hdr)
    print("-" * 105)

    win_max: list[float] = []
    win_credit: list[float] = []
    win_debit: list[float] = []

    for r in rows:
        gross = float(r["gross_pnl"] or 0)
        net = float(r["net_pnl"] or 0)
        maxp = float(r["actual_max_profit"] or r["sug_max_profit"] or 0)
        pct_max = (gross / maxp * 100) if maxp > 0 else None
        credit_rs = leg_total_credit(db, r["trade_id"])
        pct_credit = (gross / credit_rs * 100) if credit_rs > 0 else None
        # Debit strategies: credit_rs is negative; use abs as premium paid.
        pct_debit_gain = (-gross / credit_rs * 100) if credit_rs < 0 and gross > 0 else None

        closed = r["closed_on"]
        closed_s = closed.strftime("%Y-%m-%d") if closed else "—"
        name = (r["trade_name"] or r["trade_id"] or "")[:27]
        strat = (r["strategy"] or "?")[:17]
        pm = f"{pct_max:>8.1f}%" if pct_max is not None else "      n/a"
        if pct_credit is not None:
            pc = f"{pct_credit:>10.1f}%"
        elif pct_debit_gain is not None:
            pc = f"{pct_debit_gain:>9.1f}%D"
        else:
            pc = "        n/a"
        print(
            f"{name:<28} {strat:<18} {closed_s:<12} "
            f"{gross:>9,.0f} {net:>9,.0f} {maxp:>9,.0f} {pm:>9} {pc:>11}"
        )
        if gross > 0:
            if pct_max is not None:
                win_max.append(pct_max)
            if pct_credit is not None:
                win_credit.append(pct_credit)
            if pct_debit_gain is not None:
                win_debit.append(pct_debit_gain)

    print("-" * 105)
    n_wins = sum(1 for r in rows if float(r["gross_pnl"] or 0) > 0)
    print(f"Total closed: {len(rows)}  |  Winning exits (gross>0): {n_wins}")
    if win_debit:
        print(
            f"% gain on debit paid (long premium wins, %D): avg={sum(win_debit)/len(win_debit):.1f}%  "
            f"min={min(win_debit):.1f}%  max={max(win_debit):.1f}%  "
            f"median={sorted(win_debit)[len(win_debit)//2]:.1f}%"
        )
        below_50_debit = sum(1 for x in win_debit if x < 50)
        below_80_debit = sum(1 for x in win_debit if x < 80)
        print(
            f"Long-premium system default target ~80-150% debit gain: "
            f"{below_80_debit}/{len(win_debit)} wins closed below 80% debit gain"
        )
        print(
            f"Early exit band (<50% debit gain): "
            f"{below_50_debit}/{len(win_debit)} of your long-premium wins"
        )
    if win_max:
        print(
            f"% of max profit (wins): avg={sum(win_max)/len(win_max):.1f}%  "
            f"min={min(win_max):.1f}%  max={max(win_max):.1f}%  "
            f"median={sorted(win_max)[len(win_max)//2]:.1f}%"
        )
    if win_credit:
        print(
            f"% credit captured (wins): avg={sum(win_credit)/len(win_credit):.1f}%  "
            f"min={min(win_credit):.1f}%  max={max(win_credit):.1f}%  "
            f"median={sorted(win_credit)[len(win_credit)//2]:.1f}%"
        )
    below_50_max = sum(1 for x in win_max if x < 50)
    below_50_credit = sum(1 for x in win_credit if x < 50)
    if win_max:
        print(
            f"System TARGET would need 50%+ max profit: "
            f"{below_50_max}/{len(win_max)} of your wins closed below that"
        )
    if win_credit:
        print(
            f"System UI target 50% credit capture: "
            f"{below_50_credit}/{len(win_credit)} of your wins closed below that"
        )
    db.close()


if __name__ == "__main__":
    main()
