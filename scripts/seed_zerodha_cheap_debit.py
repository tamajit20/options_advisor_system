"""
Insert a cheap 2-leg NIFTY debit for a live Zerodha flow test.

Prefers a defined-risk debit vertical (BUY hedge + SELL short) when Kite
basket margin fits available funds. Otherwise falls back to a 2-lot-cheap
LONG_STRANGLE (both BUY). Max loss stays inside the account's available
margin plus the 5% funds-check buffer.

Run on VM (from repo root):
    docker compose exec options_advisor python scripts/seed_zerodha_cheap_debit.py
    docker compose exec options_advisor python scripts/seed_zerodha_cheap_debit.py --clean
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

from config import STRATEGY_CONFIG, ZERODHA_API_CONFIG, ZERODHA_EXECUTION_CONFIG
from database.connection import SQLServerConnection
from providers.zerodha.account_snapshot import fetch_account_snapshot
from providers.zerodha.execution_checks import (
    build_order_margin_params,
    check_margin_for_orders,
)
from providers.zerodha.execution_facade import KiteExecutionFacade
from providers.zerodha.facade import KiteFacade
from providers.zerodha.instruments import Instrument, InstrumentMaster
from providers.zerodha.session import is_token_valid, load_session

SID = "SUG-ZERODHA-TEST-DEBIT"
UNDERLYING = "NIFTY"
LOTS = 1
MIN_PREMIUM = 0.50
TARGET_PREMIUM = 2.00
MAX_PREMIUM = 6.00
LTP_CHUNK = 200
SPREAD_WIDTHS = (50, 100)


def _clean(db: SQLServerConnection) -> None:
    db.execute(
        "DELETE FROM options_suggestion_legs WHERE suggestion_id = ?", [SID]
    ).close()
    db.execute(
        "DELETE FROM options_suggestions WHERE suggestion_id = ?", [SID]
    ).close()
    db.commit()
    print(f"Removed {SID}")


def _chunked(items, n):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _pick_expiry(master: InstrumentMaster) -> date:
    today = date.today()
    expiries = [e for e in master.list_expiries(UNDERLYING) if e >= today]
    if not expiries:
        raise RuntimeError(f"No future {UNDERLYING} expiries in instrument master")
    for e in expiries:
        if (e - today).days >= 1:
            return e
    return expiries[0]


def _budget(usable: float) -> float:
    buffer_pct = float(ZERODHA_EXECUTION_CONFIG.get("margin_buffer_pct") or 5) / 100.0
    raw = min(usable * 0.70, usable - 25.0)
    if raw <= 0:
        raise RuntimeError(
            f"Available margin ₹{usable:.2f} is too small for any options test"
        )
    return raw / (1.0 + buffer_pct)


def _key(inst: Instrument) -> str:
    return f"{inst.exchange}:{inst.tradingsymbol}"


def _load_prices(facade: KiteFacade, chain: list[Instrument]) -> dict[str, float]:
    prices: dict[str, float] = {}
    keys = [_key(inst) for inst in chain]
    for chunk in _chunked(keys, LTP_CHUNK):
        for key, row in (facade.ltp(chunk) or {}).items():
            try:
                px = float((row or {}).get("last_price") or 0)
            except (TypeError, ValueError):
                continue
            if px > 0:
                prices[key] = px
    return prices


def _otm_candidates(
    chain: list[Instrument],
    prices: dict[str, float],
    *,
    spot: float,
    opt: str,
    budget: float,
) -> list[tuple[float, Instrument, float, float]]:
    out = []
    for inst in chain:
        if inst.instrument_type != opt:
            continue
        ltp = prices.get(_key(inst))
        if ltp is None or ltp < MIN_PREMIUM or ltp > MAX_PREMIUM:
            continue
        otm = (
            (opt == "CE" and inst.strike > spot)
            or (opt == "PE" and inst.strike < spot)
        )
        if not otm:
            continue
        cost = round(ltp * inst.lot_size * LOTS, 2)
        if cost > budget:
            continue
        otm_pct = abs(inst.strike - spot) / spot * 100.0
        score = abs(ltp - TARGET_PREMIUM) + max(0.0, otm_pct - 4.0) * 0.15
        out.append((score, inst, ltp, otm_pct))
    out.sort(key=lambda row: row[0])
    return out


def _band(price: float) -> tuple[float, float]:
    return (
        round(max(0.05, price * 0.4), 2),
        round(max(price * 2.5, price + 2.0), 2),
    )


def _ask(quotes: dict, inst: Instrument) -> float | None:
    q = quotes.get(_key(inst)) or {}
    depth = (q.get("depth") or {}).get("sell") or []
    if not depth:
        return None
    try:
        ask = float(depth[0].get("price") or 0) or None
    except (TypeError, ValueError, IndexError):
        return None
    return ask


def _seed_px(quotes: dict, inst: Instrument, ltp: float) -> float:
    return float(_ask(quotes, inst) or ltp)


def _margin_for(
    exec_facade: KiteExecutionFacade,
    legs: list[dict],
    inst_map: dict[int, Instrument],
    fallback: float,
) -> float | None:
    try:
        params = build_order_margin_params(
            legs,
            inst_map,
            transaction_fn=lambda leg: str(leg["action"]).upper(),
            limit_fn=lambda lo, inst, txn: float(next(
                lg["suggested_price"] for lg in legs if int(lg["leg_order"]) == lo
            )),
            product=ZERODHA_EXECUTION_CONFIG.get("product") or "NRML",
            variety=ZERODHA_EXECUTION_CONFIG.get("variety") or "regular",
        )
        result = check_margin_for_orders(
            exec_facade, params, fallback_required=fallback,
        )
    except Exception as exc:
        print(f"  margin check skipped: {exc}")
        return None
    print(
        f"  Kite margin: required ₹{(result.required or 0):.2f}  "
        f"available ₹{(result.available or 0):.2f}  ok={result.ok}"
        + (f"  ({result.message})" if result.message and not result.ok else "")
    )
    if not result.ok:
        return None
    return float(result.required or 0)


def _try_debit_vertical(
    *,
    chain: list[Instrument],
    prices: dict[str, float],
    spot: float,
    budget: float,
    exec_facade: KiteExecutionFacade,
    quotes_fn,
) -> dict | None:
    """BUY closer OTM + SELL further OTM. Tests hedge-then-short placement."""
    by_key = {(inst.instrument_type, inst.strike): inst for inst in chain}
    buffer_pct = float(STRATEGY_CONFIG.get("min_short_strike_buffer_pct") or 1.5)
    min_put_short = spot * (1.0 - buffer_pct / 100.0)
    max_call_short = spot * (1.0 + buffer_pct / 100.0)

    attempts = []
    for opt, strategy, long_is_higher in (
        ("PE", "BEAR_PUT_SPREAD", True),
        ("CE", "BULL_CALL_SPREAD", False),
    ):
        longs = _otm_candidates(chain, prices, spot=spot, opt=opt, budget=budget)[:12]
        for _score, long_inst, long_ltp, long_otm in longs:
            for width in SPREAD_WIDTHS:
                short_strike = (
                    long_inst.strike - width if long_is_higher
                    else long_inst.strike + width
                )
                short_inst = by_key.get((opt, float(short_strike)))
                if short_inst is None:
                    continue
                short_ltp = prices.get(_key(short_inst))
                if short_ltp is None or short_ltp < 0.20:
                    continue
                if opt == "PE" and short_inst.strike > min_put_short:
                    continue
                if opt == "CE" and short_inst.strike < max_call_short:
                    continue
                if long_ltp <= short_ltp:
                    continue
                debit = round(long_ltp - short_ltp, 2)
                max_loss = round(debit * long_inst.lot_size * LOTS, 2)
                if max_loss <= 0 or max_loss > budget:
                    continue
                attempts.append((
                    abs(long_ltp - TARGET_PREMIUM),
                    strategy,
                    long_inst,
                    long_ltp,
                    long_otm,
                    short_inst,
                    short_ltp,
                    debit,
                    max_loss,
                    width,
                ))

    attempts.sort(key=lambda row: (row[0], row[8]))
    for row in attempts[:6]:
        (
            _sc, strategy, long_inst, long_ltp, long_otm,
            short_inst, short_ltp, debit, max_loss, width,
        ) = row
        quotes = quotes_fn([long_inst, short_inst])
        long_px = _seed_px(quotes, long_inst, long_ltp)
        short_px = _seed_px(quotes, short_inst, short_ltp)
        if long_px <= short_px:
            continue
        debit = round(long_px - short_px, 2)
        max_loss = round(debit * long_inst.lot_size * LOTS, 2)
        if max_loss > budget:
            continue
        legs = [
            {
                "leg_order": 1,
                "action": "BUY",
                "strike": long_inst.strike,
                "option_type": long_inst.instrument_type,
                "lots": LOTS,
                "lot_size": long_inst.lot_size,
                "suggested_price": round(long_px, 2),
                "hedge_pair_leg": 2,
                "note": "Long hedge — flow-test debit vertical",
                "inst": long_inst,
                "ltp": long_ltp,
            },
            {
                "leg_order": 2,
                "action": "SELL",
                "strike": short_inst.strike,
                "option_type": short_inst.instrument_type,
                "lots": LOTS,
                "lot_size": short_inst.lot_size,
                "suggested_price": round(short_px, 2),
                "hedge_pair_leg": 1,
                "note": "Short premium — defined risk = net debit",
                "inst": short_inst,
                "ltp": short_ltp,
            },
        ]
        inst_map = {1: long_inst, 2: short_inst}
        print(
            f"  trying {strategy} BUY {int(long_inst.strike)} / "
            f"SELL {int(short_inst.strike)}  debit ₹{debit:.2f}  "
            f"max loss ~₹{max_loss:.0f}"
        )
        required = _margin_for(exec_facade, legs, inst_map, max_loss)
        if required is None or required > budget:
            continue
        return {
            "strategy": strategy,
            "strategy_type": "BUYING",
            "legs": legs,
            "net_credit": -debit,
            "max_loss": max_loss,
            "max_profit": round(
                (width - debit) * long_inst.lot_size * LOTS, 2
            ),
            "required": required,
            "detail": (
                f"TEST ONLY — 1-lot {UNDERLYING} {strategy.replace('_', ' ').title()} "
                f"{int(long_inst.strike)}/{int(short_inst.strike)} "
                f"{long_inst.instrument_type} to verify the 2-leg Zerodha place flow.\n"
                f"Net debit ~₹{debit:.2f} × {long_inst.lot_size} = max loss ~₹{max_loss:.0f}.\n"
                f"Kite required margin ~₹{required:.0f}. Square off after fills if this "
                f"was only a flow test."
            ),
            "trade_name": (
                f"{UNDERLYING} test {int(long_inst.strike)}/"
                f"{int(short_inst.strike)} {long_inst.instrument_type}"
            ),
        }
    return None


def _build_strangle(
    *,
    chain: list[Instrument],
    prices: dict[str, float],
    spot: float,
    budget: float,
    quotes_fn,
    exec_facade: KiteExecutionFacade,
) -> dict | None:
    puts = _otm_candidates(chain, prices, spot=spot, opt="PE", budget=budget)
    calls = _otm_candidates(chain, prices, spot=spot, opt="CE", budget=budget)
    pairs = []
    for p_score, p_inst, p_ltp, p_otm in puts[:10]:
        for c_score, c_inst, c_ltp, c_otm in calls[:10]:
            cost = round((p_ltp + c_ltp) * p_inst.lot_size * LOTS, 2)
            if cost > budget:
                continue
            pairs.append((
                p_score + c_score,
                p_inst,
                p_ltp,
                p_otm,
                c_inst,
                c_ltp,
                c_otm,
                cost,
            ))
    pairs.sort(key=lambda row: row[0])
    for row in pairs[:8]:
        _sc, p_inst, p_ltp, p_otm, c_inst, c_ltp, c_otm, _cost = row
        quotes = quotes_fn([p_inst, c_inst])
        p_px = _seed_px(quotes, p_inst, p_ltp)
        c_px = _seed_px(quotes, c_inst, c_ltp)
        max_loss = round((p_px + c_px) * p_inst.lot_size * LOTS, 2)
        if max_loss > budget:
            continue
        legs = [
            {
                "leg_order": 1,
                "action": "BUY",
                "strike": p_inst.strike,
                "option_type": "PE",
                "lots": LOTS,
                "lot_size": p_inst.lot_size,
                "suggested_price": round(p_px, 2),
                "hedge_pair_leg": 2,
                "note": "Long put — flow-test strangle",
                "inst": p_inst,
                "ltp": p_ltp,
            },
            {
                "leg_order": 2,
                "action": "BUY",
                "strike": c_inst.strike,
                "option_type": "CE",
                "lots": LOTS,
                "lot_size": c_inst.lot_size,
                "suggested_price": round(c_px, 2),
                "hedge_pair_leg": 1,
                "note": "Long call — flow-test strangle",
                "inst": c_inst,
                "ltp": c_ltp,
            },
        ]
        inst_map = {1: p_inst, 2: c_inst}
        print(
            f"  trying LONG_STRANGLE BUY {int(p_inst.strike)} PE / "
            f"{int(c_inst.strike)} CE  max loss ~₹{max_loss:.0f}"
        )
        required = _margin_for(exec_facade, legs, inst_map, max_loss)
        if required is None or required > budget:
            continue
        debit = round(p_px + c_px, 2)
        return {
            "strategy": "LONG_STRANGLE",
            "strategy_type": "BUYING",
            "legs": legs,
            "net_credit": -debit,
            "max_loss": max_loss,
            "max_profit": None,
            "required": required,
            "detail": (
                f"TEST ONLY — 1-lot {UNDERLYING} long strangle "
                f"{int(p_inst.strike)} PE / {int(c_inst.strike)} CE "
                f"to verify the 2-leg Zerodha place flow.\n"
                f"Premium ~₹{p_px:.2f}+₹{c_px:.2f} × {p_inst.lot_size} = "
                f"max loss ~₹{max_loss:.0f}.\n"
                f"Kite required margin ~₹{required:.0f}. Square off after fills "
                f"if this was only a flow test."
            ),
            "trade_name": (
                f"{UNDERLYING} test strangle "
                f"{int(p_inst.strike)}/{int(c_inst.strike)}"
            ),
        }
    return None


def seed(db: SQLServerConnection) -> None:
    if not ZERODHA_API_CONFIG.get("api_key"):
        raise RuntimeError("OPT_ZERODHA_API_KEY is not configured")
    session = load_session()
    if session is None or not is_token_valid(session):
        raise RuntimeError("No valid Zerodha session — log in via the dashboard key icon")

    acct = fetch_account_snapshot(force_refresh=True)
    if not acct.get("available"):
        raise RuntimeError(f"Could not read Zerodha funds: {acct.get('reason')}")
    usable = acct.get("usable_balance")
    if usable is None:
        raise RuntimeError("Zerodha available margin unavailable")
    usable = float(usable)
    budget = _budget(usable)

    facade = KiteFacade(
        api_key=ZERODHA_API_CONFIG["api_key"],
        access_token=session.access_token,
    )
    exec_facade = KiteExecutionFacade(
        api_key=ZERODHA_API_CONFIG["api_key"],
        access_token=session.access_token,
    )
    master = InstrumentMaster(loader=lambda: facade.instruments("NFO"))
    master.refresh_if_stale()

    spot_row = facade.ltp(["NSE:NIFTY 50"]).get("NSE:NIFTY 50") or {}
    spot = float(spot_row.get("last_price") or 0)
    if spot <= 0:
        raise RuntimeError("Could not fetch NIFTY spot")

    expiry = _pick_expiry(master)
    dte = (expiry - date.today()).days
    chain = [
        inst
        for inst in master.list_options(UNDERLYING, expiry)
        if inst.instrument_type in ("CE", "PE") and inst.lot_size > 0
    ]
    if not chain:
        raise RuntimeError(f"No {UNDERLYING} options for expiry {expiry}")

    prices = _load_prices(facade, chain)
    print(f"  Spot {spot:.2f}  expiry {expiry}  DTE {dte}")
    print(f"  Available margin ₹{usable:.2f}  budget ₹{budget:.2f}")

    def quotes_fn(insts: list[Instrument]) -> dict:
        return facade.quote([_key(i) for i in insts]) or {}

    picked = _try_debit_vertical(
        chain=chain,
        prices=prices,
        spot=spot,
        budget=budget,
        exec_facade=exec_facade,
        quotes_fn=quotes_fn,
    )
    if picked is None:
        print("  debit vertical did not fit available margin — trying long strangle")
        picked = _build_strangle(
            chain=chain,
            prices=prices,
            spot=spot,
            budget=budget,
            quotes_fn=quotes_fn,
            exec_facade=exec_facade,
        )
    if picked is None:
        raise RuntimeError(
            f"No 2-leg {UNDERLYING} structure on {expiry} fits budget ₹{budget:.2f} "
            f"(available margin ₹{usable:.2f})"
        )

    now = datetime.now()
    _clean(db)

    db.execute(
        """
        INSERT INTO options_suggestions
          (suggestion_id, trade_name, generated_on, strategy, strategy_type,
           underlying, expiry_date, dte, spot_at_generation, confidence_score,
           conditions_json, status, entry_date,
           net_credit_suggested, max_profit, max_loss,
           execution_window, plain_english)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            SID,
            picked["trade_name"],
            now,
            picked["strategy"],
            picked["strategy_type"],
            UNDERLYING,
            expiry,
            dte,
            spot,
            5,
            json.dumps([
                {
                    "label": "manual_flow_test",
                    "passed": True,
                    "detail": "Cheap 2-leg debit to verify Zerodha place flow",
                }
            ]),
            "PENDING",
            date.today(),
            picked["net_credit"],
            picked["max_profit"],
            picked["max_loss"],
            "09:15 – 15:20 IST",
            picked["detail"],
        ],
    ).close()

    for leg in picked["legs"]:
        lo, hi = _band(float(leg["suggested_price"]))
        db.execute(
            """
            INSERT INTO options_suggestion_legs
              (suggestion_id, leg_order, hedge_pair_leg, symbol, expiry_date,
               strike, option_type, action, lots, lot_size, suggested_price,
               suggested_price_low, suggested_price_high, leg_purpose_note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                SID,
                leg["leg_order"],
                leg.get("hedge_pair_leg"),
                UNDERLYING,
                expiry,
                leg["strike"],
                leg["option_type"],
                leg["action"],
                leg["lots"],
                leg["lot_size"],
                leg["suggested_price"],
                lo,
                hi,
                leg["note"],
            ],
        ).close()
    db.commit()

    print(f"Inserted {SID}  {picked['strategy']}")
    print(
        f"  Amount needed: ~₹{picked['max_loss']:.2f}  "
        f"Kite required: ~₹{picked['required']:.2f}"
    )
    for leg in picked["legs"]:
        print(
            f"  {leg['action']} 1 lot {UNDERLYING} {int(leg['strike'])} "
            f"{leg['option_type']} @ ~₹{leg['suggested_price']:.2f}  "
            f"(LTP {leg['ltp']:.2f})"
        )
    print("  Refresh dashboard → Suggestion tab → Place on Zerodha")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed cheap 2-leg Zerodha test suggestion")
    parser.add_argument("--clean", action="store_true", help="Remove test row only")
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
