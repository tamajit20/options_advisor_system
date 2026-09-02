"""
Insert one small NIFTY bull put spread for Zerodha execution smoke tests.

Uses live Kite instruments + LTP so strikes/expiry/prices match the market.
Max risk is one 100-pt spread × 1 lot (typically a few thousand ₹).

Run on VM (from repo root):
    docker compose exec options_advisor python scripts/seed_zerodha_demo_suggestion.py
    docker compose exec options_advisor python scripts/seed_zerodha_demo_suggestion.py --clean
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import ZERODHA_API_CONFIG
from database.connection import SQLServerConnection
from lifecycle.zerodha_executor import _kite_symbol_key, _resolve_instrument
from providers.zerodha.facade import KiteFacade
from providers.zerodha.instruments import InstrumentMaster
from providers.zerodha.session import is_token_valid, load_session

SID = "SUG-ZERODHA-DEMO"
UNDERLYING = "NIFTY"
LOT_SIZE = 75
SPREAD_WIDTH = 100
LOTS = 1


def _clean(db: SQLServerConnection) -> None:
    db.execute(
        "DELETE FROM options_suggestion_legs WHERE suggestion_id = ?", [SID]
    ).close()
    db.execute(
        "DELETE FROM options_suggestions WHERE suggestion_id = ?", [SID]
    ).close()
    db.commit()
    print(f"Removed {SID}")


def _nearest_expiry(master: InstrumentMaster, min_dte: int = 7) -> date:
    today = date.today()
    expiries = [e for e in master.list_expiries(UNDERLYING) if e >= today]
    if not expiries:
        raise RuntimeError(f"No future {UNDERLYING} expiries in instrument master")
    for e in expiries:
        if (e - today).days >= min_dte:
            return e
    return expiries[0]


def _pick_strikes(spot: float) -> tuple[float, float]:
    """Far OTM short put (~4% below spot) + 100-pt long hedge."""
    short_strike = round(spot * 0.96 / 50) * 50
    long_strike = short_strike - SPREAD_WIDTH
    if long_strike <= 0:
        raise RuntimeError(f"Invalid strikes for spot {spot}")
    return short_strike, long_strike


def _ltp(facade, master: InstrumentMaster, expiry: date,
         strike: float, opt: str) -> float:
    leg = {
        "symbol": UNDERLYING,
        "expiry_date": expiry,
        "strike": strike,
        "option_type": opt,
    }
    inst = _resolve_instrument(leg, master)
    key = _kite_symbol_key(inst)
    row = facade.ltp([key]).get(key) or {}
    ltp = float(row.get("last_price") or 0)
    if ltp <= 0:
        raise RuntimeError(f"No LTP for {key}")
    return round(ltp, 2)


def seed(db: SQLServerConnection) -> None:
    if not ZERODHA_API_CONFIG.get("api_key"):
        raise RuntimeError("OPT_ZERODHA_API_KEY is not configured")
    session = load_session()
    if session is None or not is_token_valid(session):
        raise RuntimeError("No valid Zerodha session — log in via WS Monitor first")

    facade = KiteFacade(
        api_key=ZERODHA_API_CONFIG["api_key"],
        access_token=session.access_token,
    )
    master = InstrumentMaster(loader=lambda: facade.instruments("NFO"))
    master.refresh_if_stale()

    spot_row = facade.ltp(["NSE:NIFTY 50"]).get("NSE:NIFTY 50") or {}
    spot = float(spot_row.get("last_price") or 0)
    if spot <= 0:
        raise RuntimeError("Could not fetch NIFTY spot")

    expiry = _nearest_expiry(master)
    dte = (expiry - date.today()).days
    short_k, long_k = _pick_strikes(spot)

    short_ltp = _ltp(facade, master, expiry, short_k, "PE")
    long_ltp = _ltp(facade, master, expiry, long_k, "PE")
    net_credit = round(short_ltp - long_ltp, 2)
    if net_credit <= 0:
        raise RuntimeError(
            f"Spread credit non-positive ({net_credit}) — pick different strikes"
        )

    max_loss_per_unit = SPREAD_WIDTH - net_credit
    max_profit_rs = round(net_credit * LOT_SIZE * LOTS, 2)
    max_loss_rs = round(max_loss_per_unit * LOT_SIZE * LOTS, 2)
    lower_be = short_k - net_credit
    now = datetime.now()

    _clean(db)

    db.execute(
        """
        INSERT INTO options_suggestions
          (suggestion_id, trade_name, generated_on, strategy, strategy_type,
           underlying, expiry_date, dte, spot_at_generation, confidence_score,
           conditions_json, status, entry_date,
           net_credit_suggested, max_profit, max_loss,
           lower_breakeven, stop_loss_level,
           probability_of_profit, estimated_charges_total, estimated_net_pnl,
           execution_window, plain_english)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            SID,
            f"{UNDERLYING} Zerodha demo bull put {int(long_k)}/{int(short_k)}",
            now,
            "BULL_PUT_SPREAD",
            "WRITING",
            UNDERLYING,
            expiry,
            dte,
            spot,
            5,
            json.dumps([{"label": "demo", "passed": True, "detail": "Zerodha smoke test"}]),
            "PENDING",
            date.today(),
            net_credit,
            max_profit_rs,
            max_loss_rs,
            lower_be,
            short_k - 80,
            70.0,
            180.0,
            round(max_profit_rs - 180.0, 2),
            "09:15 – 15:20 IST",
            (
                f"Demo suggestion for Zerodha execution testing only.\n"
                f"Sell {int(short_k)} PE / Buy {int(long_k)} PE for ~₹{net_credit}/unit.\n"
                f"Max profit ~₹{max_profit_rs:.0f}, max loss ~₹{max_loss_rs:.0f} (1 lot).\n"
                f"Prices seeded from live LTP at {now.strftime('%H:%M')}."
            ),
        ],
    ).close()

    legs = [
        (1, short_k, "SELL", short_ltp, "Short put — demo premium leg"),
        (2, long_k, "BUY", long_ltp, "Long put — caps loss at spread width"),
    ]
    for order, strike, action, price, note in legs:
        band_lo = round(max(0.05, price * 0.5), 2)
        band_hi = round(price * 2.0, 2)
        db.execute(
            """
            INSERT INTO options_suggestion_legs
              (suggestion_id, leg_order, symbol, expiry_date, strike, option_type,
               action, lots, lot_size, suggested_price,
               suggested_price_low, suggested_price_high, leg_purpose_note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                SID, order, UNDERLYING, expiry, strike, "PE",
                action, LOTS, LOT_SIZE, price, band_lo, band_hi, note,
            ],
        ).close()

    db.commit()
    print(f"Inserted {SID}")
    print(f"  Spot: {spot:.2f}  Expiry: {expiry}  DTE: {dte}")
    print(f"  Legs: SELL {short_k} PE @ {short_ltp}  |  BUY {long_k} PE @ {long_ltp}")
    print(f"  Credit: ₹{net_credit}/u  Max loss: ~₹{max_loss_rs:.0f}  (1 lot)")
    print("  Refresh dashboard → Suggestion tab")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Zerodha demo suggestion")
    parser.add_argument("--clean", action="store_true", help="Remove demo row only")
    args = parser.parse_args()

    db = SQLServerConnection()
    db.connect()
    try:
        if args.clean:
            _clean(db)
            return 0
        seed(db)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
