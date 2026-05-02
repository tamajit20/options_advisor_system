"""
seed_today_suggestions.py
=========================

Inserts 6 PENDING suggestions for TODAY covering every strategy type,
one per strategy.  No trades are created — execute them yourself from
the dashboard to test the full lifecycle.

  SUG-TODAY-001  NIFTY      IRON_CONDOR       WRITING  PENDING
  SUG-TODAY-002  NIFTY      BULL_PUT_SPREAD    WRITING  PENDING
  SUG-TODAY-003  BANKNIFTY  BEAR_CALL_SPREAD   WRITING  PENDING
  SUG-TODAY-004  NIFTY      IRON_BUTTERFLY     WRITING  PENDING
  SUG-TODAY-005  BANKNIFTY  LONG_STRADDLE      BUYING   PENDING
  SUG-TODAY-006  FINNIFTY   LONG_STRANGLE      BUYING   PENDING

Run:
    python seed_today_suggestions.py            # clean then insert
    python seed_today_suggestions.py --clean    # remove test rows only
"""

from __future__ import annotations

import argparse
import sys
import os
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database.connection import SQLServerConnection

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TODAY_IDS = [f"SUG-TODAY-00{i}" for i in range(1, 7)]

NOW    = datetime.now()
TODAY  = date.today()

NIFTY_SPOT   = 24_200.0
BNF_SPOT     = 51_800.0
FN_SPOT      = 23_400.0
EXPIRY       = date(2026, 5, 28)   # last Thursday of May 2026 (monthly)
DTE          = (EXPIRY - TODAY).days

NIFTY_LOT = 75
BNF_LOT   = 35
FN_LOT    = 65


# ---------------------------------------------------------------------------
# Helpers (mirrors seed_test_data.py)
# ---------------------------------------------------------------------------

def _clean(db):
    print("Cleaning existing SUG-TODAY-* data …")
    for sid in TODAY_IDS:
        db.execute("DELETE FROM options_suggestion_legs WHERE suggestion_id = ?", [sid]).close()
        db.execute("DELETE FROM options_suggestions      WHERE suggestion_id = ?", [sid]).close()
    db.commit()
    print("Clean done.")


def _insert_suggestion(db, sid, trade_name, strategy, strategy_type,
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
        [sid, trade_name, NOW, strategy, strategy_type,
         underlying, expiry, dte, spot, confidence,
         '{"conditions":["PCR neutral","IV rank optimal","VIX stable","ATR manageable","Trend clear","EM low","Max-pain aligned"]}',
         status, net_credit, max_profit, max_loss, upper_be, lower_be,
         sl_level, pop, charges, est_net_pnl, exec_window, plain_english],
    ).close()


def _insert_legs(db, sid, legs):
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
             leg["price"], round(leg["price"] * 0.95, 1), round(leg["price"] * 1.05, 1),
             leg["note"]],
        ).close()


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

def seed(db):
    print(f"Seeding 6 suggestions for today ({TODAY})  DTE={DTE} …\n")

    # -----------------------------------------------------------------------
    # 1. NIFTY — IRON_CONDOR — WRITING — PENDING
    # -----------------------------------------------------------------------
    sid = "SUG-TODAY-001"
    _insert_suggestion(
        db, sid,
        trade_name="NIFTY Iron Condor 23700/23800/24600/24700",
        strategy="IRON_CONDOR", strategy_type="WRITING",
        underlying="NIFTY", expiry=EXPIRY, dte=DTE,
        spot=NIFTY_SPOT, confidence=7, status="PENDING",
        net_credit=118.0,
        max_profit=round(118.0 * NIFTY_LOT, 2),           # 8 850
        max_loss=round((100.0 - 118.0 + 100.0) * NIFTY_LOT, 2),
        # wing width 100 each side; net 118 > 100 so max-loss is based on one side:
        # worst case = wing_width - net_credit (one side) but for IC both spreads are 100-wide:
        # max_loss = (100 - 62) * lot + (100 - 56) * lot? Let me keep it simple:
        # max_loss = (100 - net_credit) * lot for the bigger side
        upper_be=24600.0 + 118.0,    # 24 718
        lower_be=23800.0 - 118.0,    # 23 682
        sl_level=24680.0,
        pop=68.0, charges=425.0, est_net_pnl=round(118.0 * NIFTY_LOT - 425.0, 2),
        exec_window="09:20 – 09:45 IST",
        plain_english=(
            f"NIFTY is range-bound with IV rank 62%. Selling an Iron Condor:\n"
            f"• Sell 24600 CE / Buy 24700 CE — ₹62 credit\n"
            f"• Sell 23800 PE / Buy 23700 PE — ₹56 credit\n"
            f"Total credit ₹118/unit. Max profit if Nifty stays 23800–24600 at expiry.\n\n"
            f"ENTRY\n"
            f"• Execute 09:20–09:45 between ₹112–₹124 combined credit\n\n"
            f"TIMELINE\n"
            f"• Target 50% profit (₹59 decay) around day 10–14\n"
            f"• Call-side SL: exit call spread if Nifty rises above 24680 (80 pts above short call 24600)\n"
            f"• Put-side SL: exit put spread if Nifty falls below 23720 (80 pts below short put 23800)\n\n"
            f"All 7 confidence checks passed."
        ),
    )
    _insert_legs(db, sid, [
        {"order": 1, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 24600, "opt": "CE",
         "action": "SELL", "lots": 1, "lot_size": NIFTY_LOT, "price": 62.0,
         "hedge": None, "note": "Short call spread upper wing — capped upside risk"},
        {"order": 2, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 24700, "opt": "CE",
         "action": "BUY",  "lots": 1, "lot_size": NIFTY_LOT, "price": 18.0,
         "hedge": None, "note": "Long call hedge — limits loss above 24700"},
        {"order": 3, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 23800, "opt": "PE",
         "action": "SELL", "lots": 1, "lot_size": NIFTY_LOT, "price": 56.0,
         "hedge": None, "note": "Short put spread lower wing — capped downside risk"},
        {"order": 4, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 23700, "opt": "PE",
         "action": "BUY",  "lots": 1, "lot_size": NIFTY_LOT, "price": 14.0,
         "hedge": None, "note": "Long put hedge — limits loss below 23700"},
    ])
    print(f"  {sid}  NIFTY  IRON_CONDOR  ✓")

    # -----------------------------------------------------------------------
    # 2. NIFTY — BULL_PUT_SPREAD — WRITING — PENDING
    # -----------------------------------------------------------------------
    sid = "SUG-TODAY-002"
    _insert_suggestion(
        db, sid,
        trade_name="NIFTY Bull Put Spread 23900/24000",
        strategy="BULL_PUT_SPREAD", strategy_type="WRITING",
        underlying="NIFTY", expiry=EXPIRY, dte=DTE,
        spot=NIFTY_SPOT, confidence=7, status="PENDING",
        net_credit=64.0,
        max_profit=round(64.0 * NIFTY_LOT, 2),           # 4 800
        max_loss=round((100.0 - 64.0) * NIFTY_LOT, 2),   # 2 700
        upper_be=None,
        lower_be=24000.0 - 64.0,                          # 23 936
        sl_level=23880.0,
        pop=72.0, charges=220.0, est_net_pnl=round(64.0 * NIFTY_LOT - 220.0, 2),
        exec_window="09:20 – 10:00 IST",
        plain_english=(
            f"Mildly bullish — PCR 0.72, Nifty above 50-DMA, max pain at 24000.\n"
            f"Selling put spread:\n"
            f"• Sell 24000 PE / Buy 23900 PE for ₹64 credit\n"
            f"Profit if Nifty stays above 23936 at expiry.\n\n"
            f"ENTRY\n"
            f"• Execute at open for ₹60–₹68 combined credit\n\n"
            f"TIMELINE\n"
            f"• Target 60% profit (₹38.4 decay) by day 14\n"
            f"• Close if Nifty breaks 23880\n\n"
            f"All 7 confidence checks passed."
        ),
    )
    _insert_legs(db, sid, [
        {"order": 1, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 24000, "opt": "PE",
         "action": "SELL", "lots": 1, "lot_size": NIFTY_LOT, "price": 88.0,
         "hedge": None, "note": "Short put — collect premium, bullish anchor"},
        {"order": 2, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 23900, "opt": "PE",
         "action": "BUY",  "lots": 1, "lot_size": NIFTY_LOT, "price": 24.0,
         "hedge": None, "note": "Long put hedge — defines max loss"},
    ])
    print(f"  {sid}  NIFTY  BULL_PUT_SPREAD  ✓")

    # -----------------------------------------------------------------------
    # 3. BANKNIFTY — BEAR_CALL_SPREAD — WRITING — PENDING
    # -----------------------------------------------------------------------
    sid = "SUG-TODAY-003"
    _insert_suggestion(
        db, sid,
        trade_name="BANKNIFTY Bear Call Spread 52100/52200",
        strategy="BEAR_CALL_SPREAD", strategy_type="WRITING",
        underlying="BANKNIFTY", expiry=EXPIRY, dte=DTE,
        spot=BNF_SPOT, confidence=7, status="PENDING",
        net_credit=58.0,
        max_profit=round(58.0 * BNF_LOT, 2),              # 2 030
        max_loss=round((100.0 - 58.0) * BNF_LOT, 2),      # 1 470
        upper_be=52100.0 + 58.0,                           # 52 158
        lower_be=None,
        sl_level=52250.0,
        pop=66.0, charges=145.0, est_net_pnl=round(58.0 * BNF_LOT - 145.0, 2),
        exec_window="09:20 – 09:50 IST",
        plain_english=(
            f"BankNifty shows bearish tilt — PCR 0.58, trading below 20-DMA.\n"
            f"Selling call spread:\n"
            f"• Sell 52100 CE / Buy 52200 CE for ₹58 credit\n"
            f"Profit if BankNifty stays below 52158 at expiry.\n\n"
            f"ENTRY\n"
            f"• Execute 09:20–09:50 for ₹54–₹62 combined credit\n\n"
            f"TIMELINE\n"
            f"• Target 50% profit (₹29 decay) by day 10\n"
            f"• Close immediately if BankNifty crosses 52250\n\n"
            f"All 7 confidence checks passed."
        ),
    )
    _insert_legs(db, sid, [
        {"order": 1, "symbol": "BANKNIFTY", "expiry": EXPIRY, "strike": 52100, "opt": "CE",
         "action": "SELL", "lots": 1, "lot_size": BNF_LOT, "price": 82.0,
         "hedge": None, "note": "Short call — bearish anchor, primary premium leg"},
        {"order": 2, "symbol": "BANKNIFTY", "expiry": EXPIRY, "strike": 52200, "opt": "CE",
         "action": "BUY",  "lots": 1, "lot_size": BNF_LOT, "price": 24.0,
         "hedge": None, "note": "Long call hedge — defines max loss above 52200"},
    ])
    print(f"  {sid}  BANKNIFTY  BEAR_CALL_SPREAD  ✓")

    # -----------------------------------------------------------------------
    # 4. NIFTY — IRON_BUTTERFLY — WRITING — PENDING
    # Wings are wide (1200 pts each side) so net credit < wing width
    # -----------------------------------------------------------------------
    sid = "SUG-TODAY-004"
    # ATM body at 24200; wings at 23000 PE / 25400 CE (1200 wide)
    # ATM short straddle: ~870 total (440 CE + 430 PE)
    # Wing hedges (far OTM): ~12 CE + ~10 PE
    # Net credit: 870 - 22 = 848; wing width = 1200 → max_loss = (1200 - 848) = 352
    net_credit_ibf = 848.0
    wing_width     = 1200.0
    _insert_suggestion(
        db, sid,
        trade_name="NIFTY Iron Butterfly 23000/24200/24200/25400",
        strategy="IRON_BUTTERFLY", strategy_type="WRITING",
        underlying="NIFTY", expiry=EXPIRY, dte=DTE,
        spot=NIFTY_SPOT, confidence=7, status="PENDING",
        net_credit=net_credit_ibf,
        max_profit=round(net_credit_ibf * NIFTY_LOT, 2),                    # 63 600
        max_loss=round((wing_width - net_credit_ibf) * NIFTY_LOT, 2),       # 26 400
        upper_be=24200.0 + net_credit_ibf,    # 25 048 (not meaningful for display but kept)
        lower_be=24200.0 - net_credit_ibf,    # 23 352
        sl_level=25000.0,
        pop=48.0, charges=380.0,
        est_net_pnl=round(net_credit_ibf * NIFTY_LOT - 380.0, 2),
        exec_window="09:20 – 09:45 IST",
        plain_english=(
            f"IV rank very high (78%) — ideal for Iron Butterfly at ATM.\n"
            f"Sell ATM straddle + buy far-OTM wings:\n"
            f"• Sell 24200 CE @ ₹440 / Buy 25400 CE @ ₹12\n"
            f"• Sell 24200 PE @ ₹430 / Buy 23000 PE @ ₹10\n"
            f"Net credit ₹848/unit. Max profit if Nifty pins 24200 at expiry.\n\n"
            f"ENTRY\n"
            f"• Execute 09:20–09:45 for ₹820–₹875 combined credit\n\n"
            f"TIMELINE\n"
            f"• Theta-heavy — peak value in final 7 days; close at 40% profit\n"
            f"• Adjust if Nifty moves ±300 from 24200 intraday\n\n"
            f"All 7 confidence checks passed."
        ),
    )
    _insert_legs(db, sid, [
        {"order": 1, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 24200, "opt": "CE",
         "action": "SELL", "lots": 1, "lot_size": NIFTY_LOT, "price": 440.0,
         "hedge": None, "note": "Short ATM call — body of butterfly, main premium leg"},
        {"order": 2, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 25400, "opt": "CE",
         "action": "BUY",  "lots": 1, "lot_size": NIFTY_LOT, "price": 12.0,
         "hedge": None, "note": "Long far-OTM call wing — caps upside risk"},
        {"order": 3, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 24200, "opt": "PE",
         "action": "SELL", "lots": 1, "lot_size": NIFTY_LOT, "price": 430.0,
         "hedge": None, "note": "Short ATM put — body of butterfly, main premium leg"},
        {"order": 4, "symbol": "NIFTY", "expiry": EXPIRY, "strike": 23000, "opt": "PE",
         "action": "BUY",  "lots": 1, "lot_size": NIFTY_LOT, "price": 10.0,
         "hedge": None, "note": "Long far-OTM put wing — caps downside risk"},
    ])
    print(f"  {sid}  NIFTY  IRON_BUTTERFLY  ✓")

    # -----------------------------------------------------------------------
    # 5. BANKNIFTY — LONG_STRADDLE — BUYING — PENDING
    # -----------------------------------------------------------------------
    sid = "SUG-TODAY-005"
    debit_ls = 1080.0   # total debit (CE + PE)
    _insert_suggestion(
        db, sid,
        trade_name="BANKNIFTY Long Straddle 51800 (May-28)",
        strategy="LONG_STRADDLE", strategy_type="BUYING",
        underlying="BANKNIFTY", expiry=EXPIRY, dte=DTE,
        spot=BNF_SPOT, confidence=5, status="PENDING",
        net_credit=-debit_ls,                               # debit = negative
        max_profit=9_999_999.0,                             # theoretically unlimited
        max_loss=round(debit_ls * BNF_LOT, 2),             # 37 800
        upper_be=BNF_SPOT + debit_ls,                      # 52 880
        lower_be=BNF_SPOT - debit_ls,                      # 50 720
        sl_level=None,
        pop=40.0, charges=185.0,
        est_net_pnl=round(-debit_ls * BNF_LOT - 185.0, 2),
        exec_window="09:20 – 09:45 IST",
        plain_english=(
            f"IV rank very low (18%) + RBI rate decision expected this week.\n"
            f"Buying straddle to capture the anticipated large move:\n"
            f"• Buy 51800 CE @ ₹560 + Buy 51800 PE @ ₹520\n"
            f"Total debit ₹1080/unit. Profit if BankNifty moves >₹1080 either way.\n\n"
            f"ENTRY\n"
            f"• Execute 09:20–09:45; avoid if combined debit > ₹1140\n\n"
            f"TIMELINE\n"
            f"• Event-driven — hold through the event; exit within 2 sessions after\n"
            f"• Cut 50% loss if underlying stays flat for 3 days post-event\n\n"
            f"5/7 confidence checks passed (buying path — IV rank & DTE criteria met)."
        ),
    )
    _insert_legs(db, sid, [
        {"order": 1, "symbol": "BANKNIFTY", "expiry": EXPIRY, "strike": 51800, "opt": "CE",
         "action": "BUY", "lots": 1, "lot_size": BNF_LOT, "price": 560.0,
         "hedge": None, "note": "Long ATM call — profits if market rallies sharply"},
        {"order": 2, "symbol": "BANKNIFTY", "expiry": EXPIRY, "strike": 51800, "opt": "PE",
         "action": "BUY", "lots": 1, "lot_size": BNF_LOT, "price": 520.0,
         "hedge": None, "note": "Long ATM put — profits if market falls sharply"},
    ])
    print(f"  {sid}  BANKNIFTY  LONG_STRADDLE  ✓")

    # -----------------------------------------------------------------------
    # 6. FINNIFTY — LONG_STRANGLE — BUYING — PENDING
    # -----------------------------------------------------------------------
    sid = "SUG-TODAY-006"
    debit_ln = 228.0    # total debit (OTM CE + OTM PE)
    _insert_suggestion(
        db, sid,
        trade_name="FINNIFTY Long Strangle 23200/23600 (May-28)",
        strategy="LONG_STRANGLE", strategy_type="BUYING",
        underlying="FINNIFTY", expiry=EXPIRY, dte=DTE,
        spot=FN_SPOT, confidence=5, status="PENDING",
        net_credit=-debit_ln,
        max_profit=9_999_999.0,
        max_loss=round(debit_ln * FN_LOT, 2),              # 14 820
        upper_be=23600.0 + debit_ln,                       # 23 828
        lower_be=23200.0 - debit_ln,                       # 22 972
        sl_level=None,
        pop=38.0, charges=180.0,
        est_net_pnl=round(-debit_ln * FN_LOT - 180.0, 2),
        exec_window="09:20 – 09:50 IST",
        plain_english=(
            f"FinNifty IV rank near 6-month low (15%). Sector move expected\n"
            f"as banking earnings season begins. Buying strangle:\n"
            f"• Buy 23600 CE @ ₹132 (OTM call)\n"
            f"• Buy 23200 PE @ ₹96 (OTM put)\n"
            f"Total debit ₹228/unit. Profit if FinNifty moves outside 22972–23828.\n\n"
            f"ENTRY\n"
            f"• Execute 09:20–09:50; combined debit cap ₹250\n\n"
            f"TIMELINE\n"
            f"• Hold for 3–5 sessions around earnings announcements\n"
            f"• Exit if combined premium halves (₹114) before event\n\n"
            f"5/7 confidence checks passed (buying path — IV rank & event criteria met)."
        ),
    )
    _insert_legs(db, sid, [
        {"order": 1, "symbol": "FINNIFTY", "expiry": EXPIRY, "strike": 23600, "opt": "CE",
         "action": "BUY", "lots": 1, "lot_size": FN_LOT, "price": 132.0,
         "hedge": None, "note": "Long OTM call — profits on sharp upside move"},
        {"order": 2, "symbol": "FINNIFTY", "expiry": EXPIRY, "strike": 23200, "opt": "PE",
         "action": "BUY", "lots": 1, "lot_size": FN_LOT, "price": 96.0,
         "hedge": None, "note": "Long OTM put — profits on sharp downside move"},
    ])
    print(f"  {sid}  FINNIFTY  LONG_STRANGLE  ✓")

    db.commit()
    print(f"\nDone. 6 suggestions inserted for {TODAY}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Seed today's test suggestions")
    parser.add_argument("--clean", action="store_true",
                        help="Remove SUG-TODAY-* rows only (no re-insert)")
    args = parser.parse_args()

    db = SQLServerConnection()
    try:
        _clean(db)
        if not args.clean:
            seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
