"""
scripts/repair_bogus_close.py
=============================

One-shot correction for trades that were CLOSED with a bogus ``exit_price``
caused by the dashboard close-suggestion endpoint reading a corrupted
``settle_price`` (≈ underlying spot) out of ``options_fo_eod``. See the fix
applied to ``dashboard/server.py`` and ``engine/exit_pricing.py``.

Usage
-----
List candidates only::

    python scripts/repair_bogus_close.py --list

Preview the repair for one or more trades (no DB writes)::

    python scripts/repair_bogus_close.py --trade TRD-20260506-002 --dry-run

Apply the repair::

    python scripts/repair_bogus_close.py --trade TRD-20260506-002 --apply

What the repair does
--------------------
For each requested trade:
  1. Reads the executed legs and the underlying close on the leg's expiry
     date from ``options_spot_eod``.
  2. Recomputes ``exit_price`` as the intrinsic settlement value
     (CE = max(0, spot-strike); PE = max(0, strike-spot)).
  3. Recomputes ``leg_pnl`` from the corrected ``exit_price`` and the
     original ``fill_price`` × lots × lot_size.
  4. Recomputes the trade ``gross_pnl`` / ``net_pnl`` keeping the existing
     ``total_charges`` untouched.
  5. Writes a CRITICAL ``DATA_REPAIR`` notification with a before/after diff
     so the change is auditable from the dashboard.

The script refuses to touch a trade whose existing legs already have
exit_price that look realistic (sanity-check: every leg's exit_price must
exceed 50% of max(strike, spot) to qualify as bogus). This means re-running
is idempotent.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from typing import List, Optional

from contracts import Notification
from database.connection import SQLServerConnection
from database.models import (
    FoEodRepo,
    NotificationRepo,
    SpotEodRepo,
    TradeRepo,
)
from engine.exit_pricing import intrinsic_value, sanitized_close_price
from utils import now_ist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("repair_bogus_close")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_candidates(db: SQLServerConnection) -> List[dict]:
    """Find CLOSED trades whose every executed leg has an exit_price that is
    clearly bogus (above 50% of max(strike, spot_on_expiry))."""
    closed = db.fetch_all(
        "SELECT trade_id, trade_name, status, executed_on, closed_on, "
        "       gross_pnl, total_charges, net_pnl "
        "FROM options_trades WHERE status = 'CLOSED' AND closed_on IS NOT NULL "
        "ORDER BY closed_on DESC",
        [],
    )
    suspects = []
    for t in closed:
        legs = db.fetch_all(
            "SELECT tl.leg_order, tl.exit_price, tl.fill_price, tl.lots_actual, "
            "       sl.symbol, sl.expiry_date, sl.strike, sl.option_type, sl.action, sl.lot_size "
            "FROM options_trade_legs tl "
            "JOIN options_suggestion_legs sl ON sl.id = tl.suggestion_leg_id "
            "WHERE tl.trade_id = ? AND tl.executed = 1 ORDER BY tl.leg_order",
            [t["trade_id"]],
        )
        if not legs:
            continue
        symbol = legs[0]["symbol"]
        expiry = legs[0]["expiry_date"]
        spot_row = SpotEodRepo(db).for_date(symbol, expiry)
        spot = float(spot_row["close_price"]) if spot_row else None
        if spot is None:
            continue
        bogus_legs = []
        for l in legs:
            exit_p = float(l["exit_price"] or 0.0)
            cap = max(float(l["strike"]), spot) * 0.5
            if exit_p > cap:
                bogus_legs.append(l)
        if bogus_legs and len(bogus_legs) == len(legs):
            suspects.append({
                "trade": t, "legs": legs, "spot": spot,
                "bogus_legs": bogus_legs,
            })
    return suspects


def _repair_trade(db: SQLServerConnection, trade_id: str, apply: bool) -> bool:
    trd = TradeRepo(db)
    trade = trd.get(trade_id)
    if trade is None:
        logger.error("trade %s not found", trade_id)
        return False
    if trade.get("status") != "CLOSED":
        logger.error("trade %s is not CLOSED (status=%s) — refusing to repair",
                     trade_id, trade.get("status"))
        return False
    legs = db.fetch_all(
        "SELECT tl.leg_order, tl.exit_price, tl.fill_price, tl.lots_actual, "
        "       tl.leg_pnl, tl.leg_charges, "
        "       sl.symbol, sl.expiry_date, sl.strike, sl.option_type, sl.action, sl.lot_size "
        "FROM options_trade_legs tl "
        "JOIN options_suggestion_legs sl ON sl.id = tl.suggestion_leg_id "
        "WHERE tl.trade_id = ? AND tl.executed = 1 ORDER BY tl.leg_order",
        [trade_id],
    )
    if not legs:
        logger.error("trade %s has no executed legs", trade_id)
        return False
    symbol = legs[0]["symbol"]
    expiry = legs[0]["expiry_date"]
    spot_row = SpotEodRepo(db).for_date(symbol, expiry)
    if spot_row is None:
        logger.error("trade %s: no spot row for %s on expiry %s — cannot repair",
                     trade_id, symbol, expiry)
        return False
    spot = float(spot_row["close_price"])
    spot_date = spot_row.get("trade_date") or expiry

    print(f"\n=== {trade_id} ({trade.get('trade_name')}) ===")
    print(f"  underlying: {symbol}   expiry: {expiry}   spot_close({spot_date}) = {spot:.2f}")
    print(f"  Before repair → gross={trade.get('gross_pnl')}  charges={trade.get('total_charges')}  net={trade.get('net_pnl')}")

    fixes = []  # (leg_order, old_exit, new_exit, old_pnl, new_pnl)
    new_gross = 0.0
    any_bogus = False
    for l in legs:
        strike = float(l["strike"])
        opt = l["option_type"]
        old_exit = float(l["exit_price"] or 0.0)
        # Use the same sanitiser the live system now uses.
        new_exit, src = sanitized_close_price(
            option_type=opt, strike=strike, raw_mid=old_exit, spot=spot,
        )
        if src == "intrinsic_fallback":
            any_bogus = True
            # The raw exit was clearly bogus. Replace with intrinsic.
            corrected_exit = intrinsic_value(opt, strike, spot)
        else:
            corrected_exit = old_exit  # already plausible — leave as-is
        fill = float(l["fill_price"] or 0.0)
        lots = int(l["lots_actual"] or 0)
        lot_size = int(l["lot_size"] or 0)
        qty = lots * lot_size
        if l["action"] == "SELL":
            new_pnl = (fill - corrected_exit) * qty
        else:
            new_pnl = (corrected_exit - fill) * qty
        new_gross += new_pnl
        old_pnl = float(l["leg_pnl"] or 0.0)
        fixes.append((l["leg_order"], opt, strike, old_exit, corrected_exit, old_pnl, new_pnl))
        print(f"  L{l['leg_order']} {l['action']} {strike}{opt} fill={fill:>9.2f} "
              f"exit {old_exit:>11.2f} → {corrected_exit:>9.2f}  "
              f"leg_pnl {old_pnl:>13.2f} → {new_pnl:>11.2f}  ({src})")

    if not any_bogus:
        print(f"  No leg looks bogus for {trade_id} — nothing to repair.")
        return False

    charges = float(trade.get("total_charges") or 0.0)
    new_net = new_gross - charges
    print(f"  After repair  → gross={new_gross:.2f}  charges={charges:.2f}  net={new_net:.2f}")
    print(f"  Δ net_pnl = {new_net - float(trade.get('net_pnl') or 0.0):+.2f}")

    if not apply:
        print("  [DRY-RUN] no DB writes performed.")
        return True

    # ---- Apply ----
    ts = now_ist()
    for leg_order, opt, strike, old_exit, new_exit, old_pnl, new_pnl in fixes:
        if abs(new_exit - old_exit) < 1e-9:
            continue
        trd.update_leg_exit(trade_id, leg_order, new_exit, ts, new_pnl)
    trd.close_trade(trade_id, gross=new_gross, charges=charges, net=new_net)

    # Audit notification.
    body_lines = [
        f"Trade {trade_id} was closed using a corrupt EOD settle_price (≈ spot).",
        f"Spot {symbol} on {spot_date}: ₹{spot:.2f}. Recomputed exits from intrinsic value:",
    ]
    for leg_order, opt, strike, old_exit, new_exit, _, _ in fixes:
        body_lines.append(
            f"  L{leg_order} {strike}{opt}: exit ₹{old_exit:.2f} → ₹{new_exit:.2f}"
        )
    body_lines.append(
        f"net_pnl: ₹{float(trade.get('net_pnl') or 0.0):.2f} → ₹{new_net:.2f}"
    )
    NotificationRepo(db).insert(Notification(
        created_at=ts,
        notif_type="DATA_REPAIR",
        severity="CRITICAL",
        title=f"Trade {trade_id} exit prices corrected",
        body="\n".join(body_lines),
        related_trade_id=trade_id,
        related_suggestion_id=trade.get("suggestion_id"),
    ))
    # SQLServerConnection does NOT autocommit — commit before returning.
    db.commit()
    print(f"  ✓ Applied. Audit notification written.")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true",
                   help="List trades that look like they were closed with bogus exit prices.")
    g.add_argument("--trade", action="append",
                   help="Repair a specific trade_id (can be given multiple times).")
    g.add_argument("--all-suspects", action="store_true",
                   help="Repair every trade flagged by --list.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Preview the repair without writing to the DB (default).")
    mode.add_argument("--apply", action="store_true",
                      help="Actually write the corrections to the DB.")
    args = ap.parse_args(argv)
    apply = bool(args.apply)

    db = SQLServerConnection()
    db.connect()
    try:
        if args.list:
            suspects = _list_candidates(db)
            if not suspects:
                print("No suspect trades found.")
                return 0
            print(f"Found {len(suspects)} suspect trade(s):")
            for s in suspects:
                t = s["trade"]
                print(f"  {t['trade_id']:24s}  {t['trade_name']:36s}  closed={t['closed_on']}  "
                      f"net_pnl={t['net_pnl']}  spot_on_expiry={s['spot']}")
            return 0

        targets: List[str]
        if args.all_suspects:
            targets = [s["trade"]["trade_id"] for s in _list_candidates(db)]
        else:
            targets = args.trade or []
        if not targets:
            print("No --trade given.")
            return 1
        any_changed = False
        for tid in targets:
            changed = _repair_trade(db, tid, apply=apply)
            any_changed = any_changed or changed
        if apply and any_changed:
            print("\nAll repairs committed.")
        elif not apply:
            print("\nDry-run only. Re-run with --apply to commit.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
