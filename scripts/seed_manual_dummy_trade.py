"""
Seed a PENDING BANKNIFTY bull put spread for manual Record fills / My Trades UI.

Default: Suggestions tab only (you click Record my fills / Record at suggested).
With --open: also records fills as a MANUAL open trade so My Trades shows it now.

Run on VM:
  docker compose --env-file .env.docker exec options_advisor \\
    python scripts/seed_manual_dummy_trade.py
  docker compose --env-file .env.docker exec options_advisor \\
    python scripts/seed_manual_dummy_trade.py --open --user-id 1
  docker compose --env-file .env.docker exec options_advisor \\
    python scripts/seed_manual_dummy_trade.py --clean
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.connection import SQLServerConnection
from utils import now_ist, today_ist

SID = "SUG-MANUAL-DUMMY"
UNDERLYING = "BANKNIFTY"
LOT_SIZE = 30
LOTS = 1
SPOT = 52_000.0  # illustrative; SL / PE copy only


def _expiry(today: date) -> date:
    """Next Tuesday at least 7 DTE (BankNifty weekly)."""
    d = today + timedelta(days=7)
    while d.weekday() != 1:
        d += timedelta(days=1)
    return d


def _clean(db: SQLServerConnection) -> None:
    # Void any open dummy trades linked to this suggestion (keep audit rows).
    rows = db.fetch_all(
        "SELECT trade_id FROM options_trades "
        "WHERE suggestion_id = ? AND status = 'ACTIVE'",
        [SID],
    ) or []
    for r in rows:
        tid = r["trade_id"]
        db.execute(
            "UPDATE options_trades SET status = 'VOID', daily_status = 'VOID' "
            "WHERE trade_id = ?",
            [tid],
        ).close()
        print(f"Voided open trade {tid}")
    db.execute(
        "DELETE FROM options_suggestion_user_state WHERE suggestion_id = ?",
        [SID],
    ).close()
    db.execute(
        "DELETE FROM options_suggestion_legs WHERE suggestion_id = ?", [SID]
    ).close()
    db.execute(
        "DELETE FROM options_suggestions WHERE suggestion_id = ?", [SID]
    ).close()
    db.commit()
    print(f"Removed {SID}")


def _default_user_id(db: SQLServerConnection) -> int:
    row = db.fetch_one(
        "SELECT TOP 1 id FROM app_users "
        "WHERE is_admin = 1 AND is_active = 1 ORDER BY id"
    )
    if row and row.get("id") is not None:
        return int(row["id"])
    row = db.fetch_one("SELECT TOP 1 id FROM app_users ORDER BY id")
    if not row or row.get("id") is None:
        raise RuntimeError("No app_users rows — cannot assign owner")
    return int(row["id"])


def seed(db: SQLServerConnection) -> date:
    today = today_ist()
    now = now_ist().replace(tzinfo=None)
    expiry = _expiry(today)
    dte = (expiry - today).days

    # ~same shape as a real BPS: short put credit ~100, long hedge ~44 → ~56/u
    short_strike = 50_500.0
    long_strike = 50_000.0
    short_px = 100.35
    long_px = 44.40
    credit_unit = round(short_px - long_px, 2)
    width = short_strike - long_strike
    max_profit = round(credit_unit * LOT_SIZE * LOTS, 2)
    max_loss = round((width - credit_unit) * LOT_SIZE * LOTS, 2)
    lower_be = round(short_strike - credit_unit, 2)
    sl_level = round(short_strike - (width * 0.5), 0)

    conditions = [
        {"label": "DTE within target band", "status": "PASS", "kind": "SOFT",
         "detail": f"DTE {dte} in band 7–45"},
        {"label": "ATM strikes liquid (spread within budget)", "status": "PASS", "kind": "HARD",
         "detail": "Dummy seed — liquid band assumed"},
        {"label": "IV Rank in actionable zone", "status": "PASS", "kind": "SOFT",
         "detail": "IV Rank 42 — credit zone"},
        {"label": "VIX stable or falling", "status": "PASS", "kind": "SOFT",
         "detail": "VIX 14.2, regime FALLING"},
        {"label": "PCR in neutral band", "status": "PASS", "kind": "SOFT",
         "detail": "PCR 0.98"},
        {"label": "OI walls visible", "status": "PASS", "kind": "SOFT",
         "detail": f"Put wall {int(long_strike)} · Call wall {int(short_strike + 500)}"},
        {"label": "Today's tape & nearby OI wall", "status": "PASS", "kind": "ADVISORY",
         "detail": "Dummy seed — sideways tape"},
        {"label": "Trend identifiable", "status": "PASS", "kind": "SOFT",
         "detail": "SIDEWAYS"},
        {"label": "IV premium vs realised vol (HV-20)", "status": "PASS", "kind": "SOFT",
         "detail": "IV/HV ratio 1.12"},
        {"label": "FII positioning aligned with trend", "status": "PASS", "kind": "SOFT",
         "detail": "Flat"},
        {"label": "OI change conviction aligned with trend", "status": "PASS", "kind": "SOFT",
         "detail": "Balanced"},
        {"label": "High-impact event this week", "status": "PASS", "kind": "ADVISORY",
         "detail": "No HIGH-impact catalyst in hold window"},
        {"label": "ATM IV trajectory", "status": "PASS", "kind": "ADVISORY",
         "detail": "Stable"},
        {"label": "Session range vs 1-day EM (quiet tape)", "status": "PASS", "kind": "ADVISORY",
         "detail": "Dummy seed"},
    ]
    pe = (
        f"DUMMY MANUAL — BANKNIFTY bull put spread for UI review.\n"
        f"Short {int(short_strike)} PE / long {int(long_strike)} PE · "
        f"~₹{credit_unit}/unit credit.\n\n"
        f"ENTRY\n"
        f"• Record my fills or Record at suggested prices (manual channel)\n\n"
        f"EXIT\n"
        f"• Target ~50% credit capture; spot SL near ₹{sl_level:,.0f}\n"
    )

    _clean(db)
    db.execute(
        """
        INSERT INTO options_suggestions
          (suggestion_id, trade_name, generated_on, strategy, strategy_type,
           underlying, expiry_date, expiry_type, dte, spot_at_generation, confidence_score,
           conditions_json, status,
           net_credit_suggested, max_profit, max_loss,
           upper_breakeven, lower_breakeven, stop_loss_level,
           probability_of_profit, estimated_charges_total, estimated_net_pnl,
           execution_window, plain_english,
           data_date, entry_date, spot_data_date, fii_data_date, vix_data_date,
           oi_pcr_change, edge_score, credit_grade,
           entry_quality_score, data_source, trigger_type, provider)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',
                ?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            SID,
            f"DUMMY | BANKNIFTY BPS {int(short_strike)}/{int(long_strike)} PE",
            now,
            "BULL_PUT_SPREAD",
            "CREDIT",
            UNDERLYING,
            expiry,
            "Weekly",
            dte,
            SPOT,
            len(conditions),
            json.dumps(conditions),
            credit_unit,
            max_profit,
            max_loss,
            None,
            lower_be,
            sl_level,
            62.0,
            100.0,
            round(max_profit - 100.0, 2),
            "09:20 – 15:00 IST",
            pe,
            today,
            today,
            today,
            today,
            today,
            1.0,
            55.0,
            "B",
            70,
            "LIVE",
            "MANUAL",
            "seed",
        ],
    ).close()

    legs = [
        (1, short_strike, "PE", "SELL", short_px,
         "Bull put spread — short put, primary premium leg"),
        (2, long_strike, "PE", "BUY", long_px,
         "Bull put spread — long put hedge, defines max loss"),
    ]
    for order, strike, opt, action, px, note in legs:
        db.execute(
            """
            INSERT INTO options_suggestion_legs
              (suggestion_id, leg_order, hedge_pair_leg, symbol, expiry_date,
               strike, option_type, action, lots, lot_size,
               suggested_price, suggested_price_low, suggested_price_high, leg_purpose_note)
            VALUES (?,?,NULL,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                SID, order, UNDERLYING, expiry, strike, opt, action, LOTS, LOT_SIZE,
                px, round(px * 0.95, 2), round(px * 1.05, 2), note,
            ],
        ).close()
    db.commit()
    print(
        f"Seeded {SID}  BULL_PUT_SPREAD  entry={today}  expiry={expiry}  "
        f"dte={dte}  credit=₹{credit_unit}/u"
    )
    return expiry


def open_as_manual_trade(db: SQLServerConnection, user_id: int) -> str:
    from lifecycle.trade_executor import mark_executed

    trade_id = mark_executed(
        db,
        SID,
        fills=[],
        spot_at_execution=SPOT,
        execute_at_suggested=True,
        execution_provider="manual",
        owner_user_id=user_id,
    )
    if not trade_id:
        raise RuntimeError("mark_executed returned no trade_id")
    db.commit()
    print(f"Opened MANUAL trade {trade_id} for user_id={user_id}")
    return trade_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clean", action="store_true", help="Remove seed suggestion (+ void open dummies)")
    ap.add_argument(
        "--open",
        action="store_true",
        help="Also record at suggested prices as an open MANUAL trade",
    )
    ap.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Owner user_id for --open (default: first admin)",
    )
    args = ap.parse_args()
    db = SQLServerConnection()
    db.connect()
    try:
        if args.clean:
            _clean(db)
            return 0
        seed(db)
        if args.open:
            uid = args.user_id if args.user_id is not None else _default_user_id(db)
            open_as_manual_trade(db, uid)
            print("Open My Trades → expand DUMMY | BANKNIFTY BPS …")
        else:
            print("Open Suggestions → expand DUMMY | BANKNIFTY BPS …")
            print("  Use Record my fills or Record at suggested prices (manual).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
