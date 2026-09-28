"""
scripts/reconcile_zerodha_close_pnl.py
======================================

Overwrite closed Zerodha-trade P&L from that trade's Kite order ids +
virtual contract note. Paper/manual trades are skipped.

Usage
-----
    python scripts/reconcile_zerodha_close_pnl.py
    python scripts/reconcile_zerodha_close_pnl.py --date 2026-09-28
    python scripts/reconcile_zerodha_close_pnl.py --date 2026-09-28 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.connection import SQLServerConnection
from lifecycle.zerodha_close_pnl import reconcile_closed_zerodha_trades_on
from lifecycle.zerodha_executor import _build_client
from utils import today_ist

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reconcile_zerodha_close_pnl")


def _parse_day(raw: str | None) -> date:
    if not raw:
        return today_ist()
    return datetime.strptime(raw, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull Kite virtual-note P&L for closed Zerodha trades on a date.",
    )
    parser.add_argument(
        "--date", dest="day", default=None,
        help="IST closed_on date YYYY-MM-DD (default: today IST)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List matching Zerodha trade ids only; do not call Kite or write",
    )
    args = parser.parse_args(argv)
    day = _parse_day(args.day)

    db = SQLServerConnection()
    db.connect()
    try:
        facade = None
        if not args.dry_run:
            facade, _master = _build_client()
        results = reconcile_closed_zerodha_trades_on(
            db, facade, day, dry_run=args.dry_run,
        )
        if not results:
            print(f"No closed Zerodha trades on {day.isoformat()}")
            return 0
        for row in results:
            tid = row["trade_id"]
            if row.get("dry_run"):
                print(f"{tid} dry-run")
                continue
            snap = row.get("snapshot") or {}
            if row.get("applied"):
                print(
                    f"{tid} applied gross={snap.get('gross_pnl')} "
                    f"charges={snap.get('total_charges')} net={snap.get('net_pnl')} "
                    f"orders={','.join(snap.get('kite_order_ids') or [])}"
                )
            else:
                print(f"{tid} skipped (kept estimate)")
        applied = sum(1 for r in results if r.get("applied"))
        print(f"Done: {applied}/{len(results)} updated for {day.isoformat()}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
