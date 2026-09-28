"""
engine/charges.py
=================

Deterministic Zerodha charge calculator. Pure function — no I/O.

The trade is described by a list of legs. Each leg has:
    action     : "BUY" or "SELL"
    price      : per-share premium
    lots       : number of lots
    lot_size   : shares per lot
    is_itm_at_expiry : optional flag (for STT on expiry exercise)
    intrinsic_at_expiry : optional float (for STT on ITM expiry intrinsic)

Returns a `ChargeBreakdown`.
"""

from __future__ import annotations

from typing import List, Mapping, Sequence

from config import ZERODHA_CONFIG
from contracts import ChargeBreakdown


def estimate_charges(legs: List[Mapping]) -> ChargeBreakdown:
    cfg = ZERODHA_CONFIG

    brokerage = 0.0
    stt = 0.0
    exchange = 0.0
    sebi = 0.0
    stamp = 0.0

    for leg in legs:
        action = (leg.get("action") or "").upper()
        price = float(leg.get("price") or 0.0)
        lots = int(leg.get("lots") or 0)
        lot_size = int(leg.get("lot_size") or 0)
        qty = lots * lot_size
        if qty <= 0 or price < 0:
            continue
        turnover = price * qty

        # 1 order per leg (1 entry + we'll account for exit separately when
        # we know what the user does). For estimate we charge entry only;
        # add a second flat brokerage for an assumed exit. Net: 2× per leg.
        brokerage += 2.0 * cfg["brokerage_per_order_inr"]

        # Exchange + SEBI on both sides (entry + exit) — same turnover
        exchange += cfg["exchange_txn_pct"] * turnover * 2.0
        sebi     += cfg["sebi_charges_pct"] * turnover * 2.0

        # Stamp duty: buy-side only
        if action == "BUY":
            stamp += cfg["stamp_duty_buy_pct"] * turnover

        # STT
        if action == "SELL":
            # Sell-side premium STT
            stt += cfg["stt_sell_premium_pct"] * turnover

        # ITM-expiry STT (if user holds to expiry and exercises)
        if leg.get("is_itm_at_expiry"):
            intrinsic = float(leg.get("intrinsic_at_expiry") or 0.0)
            stt += cfg["stt_itm_expiry_intrinsic_pct"] * intrinsic * qty

    gst = cfg["gst_pct"] * (brokerage + exchange + sebi)
    total = brokerage + stt + exchange + sebi + stamp + gst

    return ChargeBreakdown(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange=round(exchange, 2),
        sebi=round(sebi, 2),
        stamp_duty=round(stamp, 2),
        gst=round(gst, 2),
        total=round(total, 2),
    )


def estimate_charges_per_txn(legs: List[Mapping]) -> ChargeBreakdown:
    """Single-transaction charge estimator — each leg counts as ONE order.

    Use this when you have a flat list of actual buy/sell transactions already
    split into entry and exit sides (e.g. entry_legs + closing_legs).
    Unlike ``estimate_charges``, this does NOT double brokerage/exchange
    for an assumed future exit — each item in ``legs`` is one real order.

    leg keys: action, price, lots, lot_size
    """
    cfg = ZERODHA_CONFIG

    brokerage = 0.0
    stt = 0.0
    exchange = 0.0
    sebi = 0.0
    stamp = 0.0

    for leg in legs:
        action = (leg.get("action") or "").upper()
        price = float(leg.get("price") or 0.0)
        lots = int(leg.get("lots") or 0)
        lot_size = int(leg.get("lot_size") or 0)
        qty = lots * lot_size
        if qty <= 0 or price < 0:
            continue
        turnover = price * qty

        brokerage += cfg["brokerage_per_order_inr"]
        exchange  += cfg["exchange_txn_pct"] * turnover
        sebi      += cfg["sebi_charges_pct"] * turnover

        if action == "BUY":
            stamp += cfg["stamp_duty_buy_pct"] * turnover
        if action == "SELL":
            stt += cfg["stt_sell_premium_pct"] * turnover

    gst = cfg["gst_pct"] * (brokerage + exchange + sebi)
    total = brokerage + stt + exchange + sebi + stamp + gst

    return ChargeBreakdown(
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange=round(exchange, 2),
        sebi=round(sebi, 2),
        stamp_duty=round(stamp, 2),
        gst=round(gst, 2),
        total=round(total, 2),
    )


def estimated_round_trip_charges_rs(legs: Sequence[Mapping]) -> float:
    """Modeled Zerodha charges if this open book were flattened now.

    Each mapping needs ``action``, entry ``price`` (or ``fill_price``),
    ``lots``, ``lot_size``, and ``exit_price`` (live mark). Entry and the
    reversing exit both count as one order each — same as close-time
    ``estimate_charges_per_txn``.
    """
    txn: List[dict] = []
    for leg in legs:
        action = (leg.get("action") or "").upper()
        if action not in ("BUY", "SELL"):
            continue
        try:
            lots = int(leg.get("lots") or 0)
            lot_size = int(leg.get("lot_size") or 0)
            fill_raw = leg.get("price")
            if fill_raw is None:
                fill_raw = leg.get("fill_price") or 0.0
            fill = float(fill_raw)
            mark = float(leg.get("exit_price") or 0.0)
        except (TypeError, ValueError):
            continue
        if lots <= 0 or lot_size <= 0 or fill < 0 or mark < 0:
            continue
        txn.append({
            "action": action, "price": fill, "lots": lots, "lot_size": lot_size,
        })
        close_action = "BUY" if action == "SELL" else "SELL"
        txn.append({
            "action": close_action, "price": mark, "lots": lots, "lot_size": lot_size,
        })
    if not txn:
        return 0.0
    return float(estimate_charges_per_txn(txn).total or 0.0)
