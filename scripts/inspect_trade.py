"""Inspect one trade by name fragment."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.connection import SQLServerConnection
from database.models import TradeRepo
from engine.sl_threshold import effective_sl_rs

name = sys.argv[1] if len(sys.argv) > 1 else "NIFTY-BPUT"

db = SQLServerConnection()
db.connect()
try:
    rows = db.fetch_all(
        """
        SELECT t.trade_id, t.trade_name, t.status, t.daily_status, t.exit_instruction,
               t.net_credit_actual, t.actual_max_profit, t.actual_max_loss,
               t.actual_stop_loss_level, t.executed_on, t.closed_on,
               s.strategy, s.underlying, s.expiry_date, s.dte, s.entry_quality_score,
               s.stop_loss_level, s.max_profit, s.max_loss, s.net_credit_suggested
        FROM options_trades t
        LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id
        WHERE t.trade_name LIKE ?
        ORDER BY t.executed_on DESC
        """,
        [f"%{name}%"],
    )
    if not rows:
        print("NOT FOUND:", name)
        raise SystemExit(1)

    for r in rows:
        print("=== TRADE ===")
        for k, v in r.items():
            print(f"  {k}: {v}")
        sl, label = effective_sl_rs(
            strategy=r.get("strategy") or "",
            max_loss_rs=float(r.get("actual_max_loss") or 0),
        )
        print(f"  effective_sl_rs: {sl:.2f} ({label})")
        tid = r["trade_id"]
        legs = TradeRepo(db).legs_with_suggestion_info(tid)
        print("=== LEGS ===")
        for lg in legs:
            print(lg)

        ev = db.fetch_all(
            """
            SELECT TOP 10 event_type, level_name, mtm_rs, threshold_rs, spot_px,
                   message, created_at
            FROM options_trade_level_events
            WHERE trade_id = ?
            ORDER BY created_at DESC
            """,
            [tid],
        )
        if ev:
            print("=== LEVEL EVENTS ===")
            for e in ev:
                print(e)

        notes = db.fetch_all(
            """
            SELECT TOP 5 severity, notif_type, title, body, created_at
            FROM options_notifications
            WHERE trade_id = ?
            ORDER BY created_at DESC
            """,
            [tid],
        )
        if notes:
            print("=== NOTIFICATIONS ===")
            for n in notes:
                print(n)
finally:
    db.close()
