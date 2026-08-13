"""
engine/equity_charges.py
========================

Zerodha NSE equity **intraday** round-trip charge estimator (Scout module).

Rates are approximate — verify against Zerodha's charge sheet periodically.
"""

from __future__ import annotations

from config import ZERODHA_EQUITY_INTRADAY_CONFIG
from contracts import ChargeBreakdown


def _order_brokerage(turnover: float, cfg: dict) -> float:
    pct = float(cfg.get("brokerage_pct", 0.0003))
    cap = float(cfg.get("brokerage_cap_inr", 20.0))
    return min(pct * turnover, cap)


def estimate_equity_intraday_charges(
    *,
    entry: float,
    exit_px: float,
    qty: int,
) -> ChargeBreakdown:
    """Round-trip intraday equity charges for entry + exit."""
    cfg = ZERODHA_EQUITY_INTRADAY_CONFIG
    qty = max(int(qty), 1)
    entry = float(entry or 0)
    exit_px = float(exit_px or 0)
    buy_turnover = entry * qty
    sell_turnover = exit_px * qty

    brokerage = _order_brokerage(buy_turnover, cfg) + _order_brokerage(sell_turnover, cfg)
    exchange = float(cfg.get("exchange_txn_pct", 0.0)) * (buy_turnover + sell_turnover)
    sebi = float(cfg.get("sebi_charges_pct", 0.0)) * (buy_turnover + sell_turnover)
    stamp = float(cfg.get("stamp_duty_buy_pct", 0.0)) * buy_turnover
    stt = float(cfg.get("stt_sell_pct", 0.0)) * sell_turnover
    gst = float(cfg.get("gst_pct", 0.18)) * (brokerage + exchange + sebi)
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


def estimate_equity_intraday_charges_for_target(
    *,
    entry: float,
    target: float,
    qty: int,
) -> ChargeBreakdown:
    """Charges assuming exit at the planned target price."""
    return estimate_equity_intraday_charges(entry=entry, exit_px=target, qty=qty)
