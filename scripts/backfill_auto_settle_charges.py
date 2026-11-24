"""
Recompute charges/net for an AUTO_SETTLED (or closed-at-expiry) trade.

Uses the same estimate / Kite blend as exit_orchestrator auto-settle.

  python scripts/backfill_auto_settle_charges.py --trade-id TRD-20261120-002
  python scripts/backfill_auto_settle_charges.py --trade-id TRD-20261120-002 --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.connection import SQLServerConnection
from database.models import TradeRepo
from lifecycle.exit_orchestrator import _resolve_auto_settle_charges


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trade-id", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    tid = args.trade_id.strip()

    db = SQLServerConnection()
    db.connect()
    try:
        trd = TradeRepo(db)
        trade = trd.get(tid)
        if not trade:
            print(f"NOT_FOUND {tid}")
            return 1
        legs = trd.legs_with_suggestion_info(tid) or []
        exec_legs = [l for l in legs if l.get("executed") and l.get("exit_price") is not None]
        if not exec_legs:
            print(f"NO_EXITS {tid}")
            return 1
        close_by_order = {
            int(l["leg_order"]): float(l["exit_price"]) for l in exec_legs
        }
        charge_legs = []
        for l in exec_legs:
            charge_legs.append({
                "leg_order": l["leg_order"],
                "action": l.get("action"),
                "lots": l.get("lots"),
                "lots_actual": l.get("lots_actual") or l.get("lots"),
                "lot_size": l.get("lot_size"),
                "fill_price": l.get("fill_price"),
                "executed": True,
            })
        charges, src = _resolve_auto_settle_charges(
            db, tid, legs=charge_legs, close_by_order=close_by_order,
        )
        gross = float(trade.get("gross_pnl") or 0.0)
        net = round(gross - charges, 2)
        print(
            f"{tid} gross={gross} old_charges={trade.get('total_charges')} "
            f"old_net={trade.get('net_pnl')} -> charges={charges} ({src}) net={net}"
        )
        if args.dry_run:
            return 0
        trd.update_pnl(tid, gross, charges, net)
        # Stamp daily_status if still OPEN after auto-close.
        if str(trade.get("daily_status") or "").upper() != "AUTO_SETTLED":
            trd.update_status(
                tid,
                str(trade.get("status") or "CLOSED"),
                "AUTO_SETTLED",
                trade.get("exit_instruction")
                or f"Backfilled charges ({src}): ₹{charges:,.2f}",
            )
        db.commit()
        print("UPDATED")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
