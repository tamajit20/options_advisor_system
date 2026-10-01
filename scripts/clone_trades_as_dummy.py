"""
Clone yesterday's (or a given day's) trades as open MANUAL dummies.

Same entry fill prices / lots / economics; no exits. Named with a DUMMY prefix
so you can compare what-if hold performance later without touching the originals.

Run on VM:
  docker compose --env-file .env.docker exec options_advisor \\
    python scripts/clone_trades_as_dummy.py
  docker compose --env-file .env.docker exec options_advisor \\
    python scripts/clone_trades_as_dummy.py --date 2026-09-30 --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.connection import SQLServerConnection
from database.models import TradeRepo
from utils import now_ist, today_ist

DUMMY_PREFIX = "DUMMY | "


def _parse_day(raw: str | None) -> date:
    if not raw:
        return today_ist() - timedelta(days=1)
    return date.fromisoformat(raw)


def _source_trades(db: SQLServerConnection, day: date) -> list[dict]:
    """Trades executed or closed on *day*, excluding existing dummies."""
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    rows = db.fetch_all(
        """
        SELECT *
        FROM options_trades
        WHERE status <> 'VOID'
          AND (
                (executed_on >= ? AND executed_on < ?)
             OR (closed_on  >= ? AND closed_on  < ?)
          )
          AND (trade_name IS NULL OR trade_name NOT LIKE ?)
        ORDER BY executed_on, trade_id
        """,
        [start, end, start, end, DUMMY_PREFIX + "%"],
    )
    return list(rows or [])


def _src_tag(source_id: str) -> str:
    # Avoid SQL LIKE character-class metacharacters ([ ]).
    return f"src={source_id}"


def _already_cloned(db: SQLServerConnection, source_id: str) -> str | None:
    """Return existing dummy trade_id if we already cloned this source."""
    tag = _src_tag(source_id)
    row = db.fetch_one(
        """
        SELECT TOP 1 trade_id FROM options_trades
        WHERE trade_name LIKE ? AND status <> 'VOID'
        ORDER BY executed_on DESC
        """,
        [f"%{tag}%"],
    )
    return (row or {}).get("trade_id")


def _dummy_name(original: str | None, source_id: str) -> str:
    base = (original or source_id).strip()
    if base.upper().startswith("DUMMY"):
        base = base.split("|", 1)[-1].strip()
        # Drop a prior src= tag if re-cloning naming.
        if " src=" in base:
            base = base.split(" src=", 1)[0].strip()
    return f"{DUMMY_PREFIX}{base} {_src_tag(source_id)}"


def clone_one(db: SQLServerConnection, src: dict, *, dry_run: bool) -> str | None:
    trd = TradeRepo(db)
    source_id = src["trade_id"]
    existing = _already_cloned(db, source_id)
    if existing:
        print(f"  skip {source_id} — already cloned as {existing}")
        return None

    legs = trd.legs(source_id)
    if not legs:
        print(f"  skip {source_id} — no legs")
        return None

    new_id = trd.next_trade_id(today_ist())
    name = _dummy_name(src.get("trade_name"), source_id)
    print(
        f"  clone {source_id} ({src.get('status')}) → {new_id}  "
        f"fills={[float(l['fill_price']) if l.get('fill_price') is not None else None for l in legs]}"
    )
    print(f"         name: {name}")
    if dry_run:
        return new_id

    trd.insert({
        "trade_id": new_id,
        "suggestion_id": src["suggestion_id"],
        "trade_name": name,
        "executed_on": src.get("executed_on") or now_ist(),
        "position_type": src.get("position_type") or "FULL_VALID",
        "net_credit_actual": src.get("net_credit_actual"),
        "actual_max_profit": src.get("actual_max_profit"),
        "actual_max_loss": src.get("actual_max_loss"),
        "actual_upper_breakeven": src.get("actual_upper_breakeven"),
        "actual_lower_breakeven": src.get("actual_lower_breakeven"),
        "actual_stop_loss_level": src.get("actual_stop_loss_level"),
        "spot_at_execution": src.get("spot_at_execution"),
        "status": "ACTIVE",
        "daily_status": "OPEN",
        "exit_instruction": None,
        "broken_state_json": src.get("broken_state_json"),
        "gross_pnl": 0.0,
        "total_charges": 0.0,
        "net_pnl": 0.0,
        "closed_on": None,
    })

    trade_legs = []
    for lg in legs:
        if not lg.get("executed") or lg.get("fill_price") is None:
            continue
        trade_legs.append({
            "suggestion_leg_id": lg["suggestion_leg_id"],
            "leg_order": lg["leg_order"],
            "executed": True,
            "fill_price": lg["fill_price"],
            "fill_time": lg.get("fill_time") or src.get("executed_on") or now_ist(),
            "not_filled_reason": None,
            "exit_price": None,
            "exit_time": None,
            "leg_pnl": None,
            "leg_charges": None,
            "lots_actual": lg.get("lots_actual"),
        })
    if not trade_legs:
        db.execute("DELETE FROM options_trades WHERE trade_id = ?", [new_id]).close()
        print(f"  abort {new_id} — no executed fills on source")
        return None

    trd.insert_legs(new_id, trade_legs)
    trd.write_execution_provenance(
        new_id,
        execution_data_source=src.get("execution_data_source") or "LIVE",
        execution_provider="manual",
        gate_passed=True,
    )
    return new_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--date",
        help="Calendar day (YYYY-MM-DD). Default: yesterday IST.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List clones only; do not write.",
    )
    args = ap.parse_args()
    day = _parse_day(args.date)

    db = SQLServerConnection()
    db.connect()
    try:
        sources = _source_trades(db, day)
        print(f"Source day {day}: {len(sources)} trade(s)")
        if not sources:
            return 0
        created: list[str] = []
        for src in sources:
            print(
                f"- {src['trade_id']}  {src.get('status')}/{src.get('daily_status')}  "
                f"{src.get('trade_name')}  credit={src.get('net_credit_actual')}  "
                f"pnl={src.get('net_pnl')}"
            )
            tid = clone_one(db, src, dry_run=args.dry_run)
            if tid and not args.dry_run:
                created.append(tid)
        if args.dry_run:
            print("Dry run — nothing written.")
        else:
            db.commit()
            print(f"Created {len(created)} dummy trade(s): {', '.join(created) or '(none)'}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
