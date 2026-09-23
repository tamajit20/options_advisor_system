"""
Insert one PENDING suggestion that exercises the Gates & warnings panel.

Includes HARD / SOFT / ADVISORY rows (PASS + SOFT_FAIL + PASS_WARN), quiet-tape
soft fail, EM calibration note, and a lagging spot feed date.

Run on VM:
    docker compose exec options_advisor python scripts/seed_gates_review_suggestion.py
    docker compose exec options_advisor python scripts/seed_gates_review_suggestion.py --clean
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.connection import SQLServerConnection

SID = "SUG-GATES-REVIEW"
UNDERLYING = "NIFTY"
LOT_SIZE = 65
SPOT = 24_850.0


def _clean(db: SQLServerConnection) -> None:
    db.execute("DELETE FROM options_suggestion_legs WHERE suggestion_id = ?", [SID]).close()
    db.execute("DELETE FROM options_suggestions WHERE suggestion_id = ?", [SID]).close()
    db.commit()
    print(f"Removed {SID}")


def _expiry(today: date) -> date:
    # Next Tuesday at least 10 DTE (Nifty weekly).
    d = today + timedelta(days=10)
    while d.weekday() != 1:  # Tuesday
        d += timedelta(days=1)
    return d


def _conditions() -> list[dict]:
    return [
        {"label": "DTE within target band", "status": "PASS", "kind": "SOFT",
         "detail": "DTE 14 in band 7–45"},
        {"label": "ATM strikes liquid (spread within budget)", "status": "PASS", "kind": "HARD",
         "detail": "ATM mid-spread 0.8% of premium"},
        {"label": "IV Rank in actionable zone", "status": "PASS", "kind": "SOFT",
         "detail": "IV Rank 28 — buying / long-vol zone"},
        {"label": "VIX stable or falling", "status": "PASS", "kind": "SOFT",
         "detail": "VIX 13.4, regime FALLING"},
        {"label": "PCR in neutral band", "status": "PASS", "kind": "SOFT",
         "detail": "PCR 0.92"},
        {"label": "OI walls visible", "status": "PASS", "kind": "SOFT",
         "detail": "Put wall 24600 · Call wall 25200"},
        {"label": "Trend identifiable", "status": "PASS", "kind": "SOFT",
         "detail": "SIDEWAYS"},
        {"label": "IV premium vs realised vol (HV-20)", "status": "SOFT_FAIL", "kind": "SOFT",
         "detail": "IV/HV ratio 0.91 (IV 17% vs HV-20 19%) — options cheaper than realised"},
        {"label": "FII positioning aligned with trend", "status": "PASS", "kind": "SOFT",
         "detail": "FII index futures flat vs sideways tape"},
        {"label": "OI change conviction aligned with trend", "status": "PASS", "kind": "SOFT",
         "detail": "OIΔ PCR 1.05 — balanced"},
        {"label": "High-impact event this week", "status": "PASS", "kind": "ADVISORY",
         "detail": "No HIGH-impact catalyst in hold window"},
        {"label": "ATM IV trajectory", "status": "PASS_WARN", "kind": "ADVISORY",
         "detail": "ATM IV drifting lower on the day"},
        {"label": "Session range vs 1-day EM (quiet tape)", "status": "SOFT_FAIL", "kind": "ADVISORY",
         "detail": "Session range 42 pts = 28% of 1-day EM (150) — quiet tape; review before taking long vol"},
        {"label": "Long-vol IV rank / catalyst", "status": "PASS", "kind": "SOFT",
         "detail": "IV Rank 28 ≥ floor with sideways tape"},
        {"label": "Long-vol IV/HV", "status": "SOFT_FAIL", "kind": "SOFT",
         "detail": "IV/HV 0.91 above cheap ceiling — long vol edge weak"},
    ]


def seed(db: SQLServerConnection) -> None:
    today = date.today()
    now = datetime.now()
    expiry = _expiry(today)
    dte = (expiry - today).days
    debit = 186.0
    max_loss = round(debit * LOT_SIZE, 2)
    conditions = _conditions()
    passed = sum(1 for c in conditions if c["status"] == "PASS")
    pe = (
        f"NIFTY trading at {SPOT:,.0f}. Dummy LONG_STRADDLE for Gates & warnings review.\n"
        f"Strategy: buy ATM straddle (CE+PE {int(SPOT / 50) * 50}).\n\n"
        f"ENTRY\n"
        f"• Execute 09:20–10:00 IST\n\n"
        f"TIMELINE\n"
        f"• Review quiet-tape soft fail before taking\n"
        f"• Target 40% of debit recovered or exit by day 5\n\n"
        f"All {passed} confidence checks passed."
    )
    atm = int(round(SPOT / 50.0) * 50)

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
           oi_pcr_change, edge_score, credit_grade, em_calibration_warning,
           entry_quality_score, data_source, trigger_type, provider)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',
                ?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            SID,
            f"NIFTY Long Straddle {atm} (gates review)",
            now,
            "LONG_STRADDLE",
            "BUYING",
            UNDERLYING,
            expiry,
            "Weekly",
            dte,
            SPOT,
            passed,
            json.dumps(conditions),
            -debit,
            None,
            max_loss,
            atm + debit,
            atm - debit,
            round(debit * 0.5, 1),
            42.0,
            280.0,
            round(-debit * LOT_SIZE - 280.0, 2),
            "09:20 – 10:00 IST",
            pe,
            today,
            today,
            today - timedelta(days=1),  # lagging spot → NSE freshness row
            today,
            today,
            1.05,
            48.0,
            None,
            "Realised/expected median 0.72 for NIFTY 7–14 DTE — shorts may be tight",
            55,
            "LIVE",
            "MANUAL",
            "seed",
        ],
    ).close()

    legs = [
        (1, atm, "CE", "BUY", 112.0, "Long ATM call — vol expansion leg"),
        (2, atm, "PE", "BUY", 74.0, "Long ATM put — vol expansion leg"),
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
                SID, order, UNDERLYING, expiry, strike, opt, action, 1, LOT_SIZE,
                px, round(px * 0.95, 1), round(px * 1.05, 1), note,
            ],
        ).close()
    db.commit()
    print(f"Seeded {SID}  LONG_STRADDLE  entry={today}  expiry={expiry}  dte={dte}")
    print("Open Suggestion tab — Gates & warnings should show Kind/Result including quiet-tape SOFT_FAIL.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()
    db = SQLServerConnection()
    db.connect()
    try:
        if args.clean:
            _clean(db)
        else:
            seed(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
