"""
seed_test_data.py
=================

Inserts realistic test suggestions + trades covering every strategy type
and trade lifecycle state:

  SUG-TEST-001  IRON_CONDOR       WRITING   → PENDING (no trade yet)
  SUG-TEST-002  BULL_PUT_SPREAD   WRITING   → EXECUTED / ACTIVE / OPEN (holding)
  SUG-TEST-003  BEAR_CALL_SPREAD  WRITING   → EXECUTED / ACTIVE / SL_HIT exit signal
  SUG-TEST-004  LONG_STRADDLE     BUYING    → EXECUTED / CLOSED (profit booked)
  SUG-TEST-005  LONG_STRANGLE     BUYING    → EXECUTED / ACTIVE / TAKE_PROFIT signal

Run:
    python seed_test_data.py
    python seed_test_data.py --clean   # removes the test rows first
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, date

# ---------------------------------------------------------------------------
# Bootstrap path so project imports work when run from any cwd
# ---------------------------------------------------------------------------
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database.connection import SQLServerConnection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TEST_IDS = [f"SUG-TEST-00{i}" for i in range(1, 6)]
TRADE_IDS = {
    "SUG-TEST-002": "TRD-TEST-001",
    "SUG-TEST-003": "TRD-TEST-002",
    "SUG-TEST-004": "TRD-TEST-003",
    "SUG-TEST-005": "TRD-TEST-004",
}

NOW = datetime.now()
TODAY = date.today()

# Realistic NIFTY/BANKNIFTY values (approx May 2026)
NIFTY_SPOT   = 24200.0
BNF_SPOT     = 51800.0
EXPIRY_NEAR  = date(2026, 5, 29)   # monthly
EXPIRY_PAST  = date(2026, 5, 1)    # already expired (for closed trade)
NIFTY_LOT    = 75
BNF_LOT      = 35


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def dt(y, mo, d, h=19, mi=30):
    return datetime(y, mo, d, h, mi, 0)


def _clean(db: SQLServerConnection):
    print("Cleaning existing test data …")
    for tid in TRADE_IDS.values():
        db.execute("DELETE FROM options_trade_legs  WHERE trade_id = ?", [tid]).close()
        db.execute("DELETE FROM options_trades       WHERE trade_id = ?", [tid]).close()
    for sid in TEST_IDS:
        db.execute("DELETE FROM options_simulation_legs WHERE suggestion_id = ?", [sid]).close()
        db.execute("DELETE FROM options_simulations      WHERE suggestion_id = ?", [sid]).close()
        db.execute("DELETE FROM options_suggestion_legs  WHERE suggestion_id = ?", [sid]).close()
        db.execute("DELETE FROM options_suggestions      WHERE suggestion_id = ?", [sid]).close()
    db.commit()
    print("Clean done.")


def _insert_suggestion(db, sid, trade_name, generated_on, strategy, strategy_type,
                       underlying, expiry, dte, spot, confidence, status,
                       net_credit, max_profit, max_loss, upper_be, lower_be,
                       sl_level, pop, charges, est_net_pnl, exec_window, plain_english):
    db.execute(
        """
        INSERT INTO options_suggestions
          (suggestion_id, trade_name, generated_on, strategy, strategy_type,
           underlying, expiry_date, dte, spot_at_generation, confidence_score,
           conditions_json, status,
           net_credit_suggested, max_profit, max_loss,
           upper_breakeven, lower_breakeven, stop_loss_level,
           probability_of_profit, estimated_charges_total, estimated_net_pnl,
           execution_window, plain_english)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [sid, trade_name, generated_on, strategy, strategy_type,
         underlying, expiry, dte, spot, confidence,
         '{"conditions":["PCR neutral","IV rank high","VIX stable"]}', status,
         net_credit, max_profit, max_loss, upper_be, lower_be,
         sl_level, pop, charges, est_net_pnl, exec_window, plain_english],
    ).close()


def _insert_sug_legs(db, sid, legs):
    """legs: list of dicts with keys matching options_suggestion_legs columns."""
    for leg in legs:
        db.execute(
            """
            INSERT INTO options_suggestion_legs
              (suggestion_id, leg_order, hedge_pair_leg, symbol, expiry_date,
               strike, option_type, action, lots, lot_size,
               suggested_price, suggested_price_low, suggested_price_high, leg_purpose_note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [sid, leg["order"], leg.get("hedge"), leg["symbol"], leg["expiry"],
             leg["strike"], leg["opt"], leg["action"], leg["lots"], leg["lot_size"],
             leg["price"], leg["price"] * 0.95, leg["price"] * 1.05, leg["note"]],
        ).close()


def _get_leg_ids(db, sid):
    """Return {leg_order: id} for suggestion legs."""
    rows = db.fetch_all(
        "SELECT id, leg_order FROM options_suggestion_legs WHERE suggestion_id = ? ORDER BY leg_order",
        [sid],
    )
    return {r["leg_order"]: r["id"] for r in rows}


def _insert_trade(db, trade_id, sid, trade_name, executed_on, position_type,
                  net_credit, max_profit, max_loss, upper_be, lower_be,
                  sl_level, spot_exec, status, daily_status, exit_instruction=None,
                  gross_pnl=None, charges=None, net_pnl=None, closed_on=None):
    db.execute(
        """
        INSERT INTO options_trades
          (trade_id, suggestion_id, trade_name, executed_on, position_type,
           net_credit_actual, actual_max_profit, actual_max_loss,
           actual_upper_breakeven, actual_lower_breakeven, actual_stop_loss_level,
           spot_at_execution, status, daily_status, exit_instruction,
           gross_pnl, total_charges, net_pnl, closed_on)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [trade_id, sid, trade_name, executed_on, position_type,
         net_credit, max_profit, max_loss, upper_be, lower_be,
         sl_level, spot_exec, status, daily_status, exit_instruction,
         gross_pnl, charges, net_pnl, closed_on],
    ).close()


def _insert_trade_legs(db, trade_id, sid, fills, leg_ids):
    """fills: list of dicts per leg."""
    for f in fills:
        lo = f["order"]
        db.execute(
            """
            INSERT INTO options_trade_legs
              (trade_id, suggestion_leg_id, leg_order, executed,
               fill_price, fill_time, lots_actual,
               exit_price, exit_time, leg_pnl)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            [trade_id, leg_ids[lo], lo, 1 if f.get("executed", True) else 0,
             f.get("fill_price"), f.get("fill_time"), f.get("lots"),
             f.get("exit_price"), f.get("exit_time"), f.get("leg_pnl")],
        ).close()


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

def seed(db: SQLServerConnection):

    # -----------------------------------------------------------------------
    # 1. IRON_CONDOR  — WRITING — PENDING (not executed yet, shows on suggestion page)
    # -----------------------------------------------------------------------
    sid = "SUG-TEST-001"
    _insert_suggestion(
        db, sid,
        trade_name="NIFTY Iron Condor 23700/23800/24600/24700",
        generated_on=dt(2026, 5, 2, 19, 30),
        strategy="IRON_CONDOR", strategy_type="WRITING",
        underlying="NIFTY", expiry=EXPIRY_NEAR, dte=27,
        spot=NIFTY_SPOT, confidence=7, status="PENDING",
        net_credit=118.0, max_profit=8850.0, max_loss=6525.0,
        upper_be=24718.0, lower_be=23782.0,
        sl_level=24680.0, pop=68.0, charges=425.0, est_net_pnl=8425.0,
        exec_window="09:20 – 09:45 IST",
        plain_english=(
            "Market is range-bound with high IV rank (62%). Selling an Iron Condor:\n"
            "• Sell 24600 CE / Buy 24700 CE (call spread, ₹62 credit)\n"
            "• Sell 23800 PE / Buy 23700 PE (put spread, ₹56 credit)\n"
            "Total credit ₹118. Max profit if Nifty stays between 23800–24600.\n\n"
            "ENTRY THRESHOLDS\n"
            "• Execute at open between ₹112–₹124 combined credit\n\n"
            "TIMELINE\n"
            "• Target exit at 50% profit (₹59 credit decay) around day 10–14\n"
            "• Hard SL if Nifty crosses 24680 or 23820\n"
            "All 7 confidence checks passed."
        ),
    )
    _insert_sug_legs(db, sid, [
        {"order": 1, "symbol": "NIFTY", "expiry": EXPIRY_NEAR, "strike": 24600, "opt": "CE",
         "action": "SELL", "lots": 1, "lot_size": NIFTY_LOT, "price": 62.0,
         "note": "Short call spread upper wing — capped upside risk"},
        {"order": 2, "symbol": "NIFTY", "expiry": EXPIRY_NEAR, "strike": 24700, "opt": "CE",
         "action": "BUY",  "lots": 1, "lot_size": NIFTY_LOT, "price": 18.0,
         "note": "Long call hedge — limits loss above 24700"},
        {"order": 3, "symbol": "NIFTY", "expiry": EXPIRY_NEAR, "strike": 23800, "opt": "PE",
         "action": "SELL", "lots": 1, "lot_size": NIFTY_LOT, "price": 56.0,
         "note": "Short put spread lower wing — capped downside risk"},
        {"order": 4, "symbol": "NIFTY", "expiry": EXPIRY_NEAR, "strike": 23700, "opt": "PE",
         "action": "BUY",  "lots": 1, "lot_size": NIFTY_LOT, "price": 14.0,
         "note": "Long put hedge — limits loss below 23700"},
    ])
    print(f"  {sid}  IRON_CONDOR  PENDING ✓")

    # -----------------------------------------------------------------------
    # 2. BULL_PUT_SPREAD — WRITING — ACTIVE / OPEN (holding, no exit signal)
    # -----------------------------------------------------------------------
    sid = "SUG-TEST-002"
    _insert_suggestion(
        db, sid,
        trade_name="NIFTY Bull Put Spread 23900/24000",
        generated_on=dt(2026, 4, 29, 19, 30),
        strategy="BULL_PUT_SPREAD", strategy_type="WRITING",
        underlying="NIFTY", expiry=EXPIRY_NEAR, dte=30,
        spot=24050.0, confidence=6, status="EXECUTED",
        net_credit=64.0, max_profit=4800.0, max_loss=3675.0,
        upper_be=None, lower_be=23936.0,
        sl_level=23880.0, pop=72.0, charges=220.0, est_net_pnl=4580.0,
        exec_window="09:20 – 10:00 IST",
        plain_english=(
            "Mildly bullish bias. PCR 0.72 and Nifty above 50-DMA. Selling put spread:\n"
            "• Sell 24000 PE / Buy 23900 PE for ₹64 credit\n"
            "Profit if Nifty stays above 23936 at expiry.\n"
            "6/7 confidence checks passed (VIX slightly elevated)."
        ),
    )
    _insert_sug_legs(db, sid, [
        {"order": 1, "symbol": "NIFTY", "expiry": EXPIRY_NEAR, "strike": 24000, "opt": "PE",
         "action": "SELL", "lots": 2, "lot_size": NIFTY_LOT, "price": 88.0,
         "note": "Short put — collect premium, bullish anchor"},
        {"order": 2, "symbol": "NIFTY", "expiry": EXPIRY_NEAR, "strike": 23900, "opt": "PE",
         "action": "BUY",  "lots": 2, "lot_size": NIFTY_LOT, "price": 24.0,
         "note": "Long put hedge — defines max loss"},
    ])
    leg_ids = _get_leg_ids(db, sid)
    _insert_trade(
        db, "TRD-TEST-001", sid,
        trade_name="NIFTY Bull Put Spread 23900/24000",
        executed_on=dt(2026, 4, 30, 9, 35),
        position_type="FULL_VALID",
        net_credit=63.5, max_profit=9525.0, max_loss=7350.0,
        upper_be=None, lower_be=23936.5,
        sl_level=23880.0, spot_exec=24060.0,
        status="ACTIVE", daily_status="OPEN",
    )
    _insert_trade_legs(db, "TRD-TEST-001", sid, [
        {"order": 1, "fill_price": 87.5,  "fill_time": dt(2026, 4, 30, 9, 36), "lots": 2, "executed": True},
        {"order": 2, "fill_price": 24.0,  "fill_time": dt(2026, 4, 30, 9, 36), "lots": 2, "executed": True},
    ], leg_ids)
    print(f"  {sid}  BULL_PUT_SPREAD  ACTIVE/OPEN ✓")

    # -----------------------------------------------------------------------
    # 3. BEAR_CALL_SPREAD — WRITING — ACTIVE / SL_HIT (exit signal)
    # -----------------------------------------------------------------------
    sid = "SUG-TEST-003"
    _insert_suggestion(
        db, sid,
        trade_name="BANKNIFTY Bear Call Spread 51900/52000",
        generated_on=dt(2026, 4, 28, 19, 30),
        strategy="BEAR_CALL_SPREAD", strategy_type="WRITING",
        underlying="BANKNIFTY", expiry=date(2026, 5, 28), dte=30,
        spot=51600.0, confidence=6, status="EXECUTED",
        net_credit=55.0, max_profit=1925.0, max_loss=1575.0,
        upper_be=51955.0, lower_be=None,
        sl_level=52050.0, pop=65.0, charges=130.0, est_net_pnl=1795.0,
        exec_window="09:20 – 09:50 IST",
        plain_english=(
            "Mildly bearish on BankNifty. Selling call spread:\n"
            "• Sell 51900 CE / Buy 52000 CE for ₹55 credit\n"
            "Profit if BankNifty stays below 51955 at expiry.\n"
            "6/7 confidence checks passed."
        ),
    )
    _insert_sug_legs(db, sid, [
        {"order": 1, "symbol": "BANKNIFTY", "expiry": date(2026, 5, 28), "strike": 51900, "opt": "CE",
         "action": "SELL", "lots": 1, "lot_size": BNF_LOT, "price": 80.0,
         "note": "Short call — bearish anchor"},
        {"order": 2, "symbol": "BANKNIFTY", "expiry": date(2026, 5, 28), "strike": 52000, "opt": "CE",
         "action": "BUY",  "lots": 1, "lot_size": BNF_LOT, "price": 25.0,
         "note": "Long call hedge — defines max loss"},
    ])
    leg_ids = _get_leg_ids(db, sid)
    exit_instr = (
        "SL_HIT — BankNifty crossed 52050. "
        "Suggested close: Buy back 51900 CE @ ~₹120; Sell back 52000 CE @ ~₹68 | "
        "Est. P&L ₹-1085 | Record actual fills via 'Close Trade'."
    )
    _insert_trade(
        db, "TRD-TEST-002", sid,
        trade_name="BANKNIFTY Bear Call Spread 51900/52000",
        executed_on=dt(2026, 4, 29, 9, 40),
        position_type="FULL_VALID",
        net_credit=54.0, max_profit=1890.0, max_loss=1610.0,
        upper_be=51954.0, lower_be=None,
        sl_level=52050.0, spot_exec=51620.0,
        status="ACTIVE", daily_status="SL_HIT",
        exit_instruction=exit_instr,
    )
    _insert_trade_legs(db, "TRD-TEST-002", sid, [
        {"order": 1, "fill_price": 79.0,  "fill_time": dt(2026, 4, 29, 9, 41), "lots": 1, "executed": True},
        {"order": 2, "fill_price": 25.0,  "fill_time": dt(2026, 4, 29, 9, 41), "lots": 1, "executed": True},
    ], leg_ids)
    print(f"  {sid}  BEAR_CALL_SPREAD  ACTIVE/SL_HIT ✓")

    # -----------------------------------------------------------------------
    # 4. LONG_STRADDLE — BUYING — CLOSED (profit booked)
    # -----------------------------------------------------------------------
    sid = "SUG-TEST-004"
    _insert_suggestion(
        db, sid,
        trade_name="NIFTY Long Straddle 24200 (May-01)",
        generated_on=dt(2026, 4, 20, 19, 30),
        strategy="LONG_STRADDLE", strategy_type="BUYING",
        underlying="NIFTY", expiry=EXPIRY_PAST, dte=11,
        spot=24180.0, confidence=5, status="EXECUTED",
        net_credit=-182.0, max_profit=999999.0, max_loss=13650.0,
        upper_be=24382.0, lower_be=24018.0,
        sl_level=None, pop=42.0, charges=210.0, est_net_pnl=-392.0,
        exec_window="09:20 – 09:45 IST",
        plain_english=(
            "Low IV rank (22%) with event risk (RBI policy on Apr 25). Buying straddle:\n"
            "• Buy 24200 CE + Buy 24200 PE for ₹182 total debit\n"
            "Profit if Nifty moves more than ₹182 from 24200 by expiry.\n"
            "5/7 confidence checks passed (buying path)."
        ),
    )
    _insert_sug_legs(db, sid, [
        {"order": 1, "symbol": "NIFTY", "expiry": EXPIRY_PAST, "strike": 24200, "opt": "CE",
         "action": "BUY", "lots": 1, "lot_size": NIFTY_LOT, "price": 95.0,
         "note": "Long call — profits if market rallies sharply"},
        {"order": 2, "symbol": "NIFTY", "expiry": EXPIRY_PAST, "strike": 24200, "opt": "PE",
         "action": "BUY", "lots": 1, "lot_size": NIFTY_LOT, "price": 87.0,
         "note": "Long put — profits if market falls sharply"},
    ])
    leg_ids = _get_leg_ids(db, sid)
    # Nifty rallied to 24600 before expiry — CE worth ~400, PE expired ~0
    ce_exit = 185.0   # closed at 2× entry
    pe_exit = 18.0    # let go cheap
    ce_fill = 94.0
    pe_fill = 87.0
    lots = 1
    qty = lots * NIFTY_LOT
    ce_pnl = (ce_exit - ce_fill) * qty   # (185-94)*75 = 6825
    pe_pnl = (pe_exit - pe_fill) * qty   # (18-87)*75 = -5175
    gross = ce_pnl + pe_pnl              # 1650
    charges = 240.0
    net = gross - charges                # 1410
    closed_dt = dt(2026, 4, 30, 10, 15)
    _insert_trade(
        db, "TRD-TEST-003", sid,
        trade_name="NIFTY Long Straddle 24200 (May-01)",
        executed_on=dt(2026, 4, 21, 9, 35),
        position_type="FULL_VALID",
        net_credit=-181.0, max_profit=999999.0, max_loss=13575.0,
        upper_be=24381.0, lower_be=24019.0,
        sl_level=None, spot_exec=24175.0,
        status="CLOSED", daily_status=None,
        gross_pnl=gross, charges=charges, net_pnl=net,
        closed_on=closed_dt,
    )
    _insert_trade_legs(db, "TRD-TEST-003", sid, [
        {"order": 1, "fill_price": ce_fill, "fill_time": dt(2026, 4, 21, 9, 36),
         "lots": lots, "executed": True, "exit_price": ce_exit,
         "exit_time": closed_dt, "leg_pnl": ce_pnl},
        {"order": 2, "fill_price": pe_fill, "fill_time": dt(2026, 4, 21, 9, 36),
         "lots": lots, "executed": True, "exit_price": pe_exit,
         "exit_time": closed_dt, "leg_pnl": pe_pnl},
    ], leg_ids)
    print(f"  {sid}  LONG_STRADDLE  CLOSED (profit ₹{net:.0f}) ✓")

    # -----------------------------------------------------------------------
    # 5. LONG_STRANGLE — BUYING — ACTIVE / TAKE_PROFIT signal
    # -----------------------------------------------------------------------
    sid = "SUG-TEST-005"
    _insert_suggestion(
        db, sid,
        trade_name="NIFTY Long Strangle 23600P/24800C",
        generated_on=dt(2026, 4, 27, 19, 30),
        strategy="LONG_STRANGLE", strategy_type="BUYING",
        underlying="NIFTY", expiry=EXPIRY_NEAR, dte=32,
        spot=24100.0, confidence=5, status="EXECUTED",
        net_credit=-96.0, max_profit=999999.0, max_loss=7200.0,
        upper_be=24896.0, lower_be=23504.0,
        sl_level=None, pop=38.0, charges=185.0, est_net_pnl=-281.0,
        exec_window="09:20 – 09:50 IST",
        plain_english=(
            "IV rank at 25% — cheap premiums. Budget strangle:\n"
            "• Buy 24800 CE + Buy 23600 PE for ₹96 total debit\n"
            "Profit if Nifty breaks out beyond 24896 or below 23504.\n"
            "5/7 confidence checks passed."
        ),
    )
    _insert_sug_legs(db, sid, [
        {"order": 1, "symbol": "NIFTY", "expiry": EXPIRY_NEAR, "strike": 24800, "opt": "CE",
         "action": "BUY", "lots": 1, "lot_size": NIFTY_LOT, "price": 48.0,
         "note": "Long OTM call — upside breakout leg"},
        {"order": 2, "symbol": "NIFTY", "expiry": EXPIRY_NEAR, "strike": 23600, "opt": "PE",
         "action": "BUY", "lots": 1, "lot_size": NIFTY_LOT, "price": 48.0,
         "note": "Long OTM put — downside breakout leg"},
    ])
    leg_ids = _get_leg_ids(db, sid)
    exit_instr = (
        "TAKE_PROFIT — captured 60% of open profit. "
        "Suggested close: Sell back 24800 CE @ ~₹94; Sell back 23600 PE @ ~₹12 | "
        "Est. P&L ₹3450 | Record actual fills via 'Close Trade'."
    )
    _insert_trade(
        db, "TRD-TEST-004", sid,
        trade_name="NIFTY Long Strangle 23600P/24800C",
        executed_on=dt(2026, 4, 28, 9, 38),
        position_type="FULL_VALID",
        net_credit=-96.5, max_profit=999999.0, max_loss=7238.0,
        upper_be=24896.5, lower_be=23503.5,
        sl_level=None, spot_exec=24095.0,
        status="ACTIVE", daily_status="TAKE_PROFIT",
        exit_instruction=exit_instr,
    )
    _insert_trade_legs(db, "TRD-TEST-004", sid, [
        {"order": 1, "fill_price": 48.5, "fill_time": dt(2026, 4, 28, 9, 39),
         "lots": 1, "executed": True},
        {"order": 2, "fill_price": 48.0, "fill_time": dt(2026, 4, 28, 9, 39),
         "lots": 1, "executed": True},
    ], leg_ids)
    print(f"  {sid}  LONG_STRANGLE  ACTIVE/TAKE_PROFIT ✓")

    db.commit()
    print("\nAll test data inserted successfully.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed test suggestion/trade data")
    parser.add_argument("--clean", action="store_true",
                        help="Remove existing test rows before inserting")
    args = parser.parse_args()

    db = SQLServerConnection()
    db.connect()
    try:
        if args.clean:
            _clean(db)
        else:
            # Clean first to make the script idempotent, then seed
            _clean(db)
            print("Inserting test data …")
            seed(db)
    finally:
        db.close()
