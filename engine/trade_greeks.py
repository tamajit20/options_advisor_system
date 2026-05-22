"""
engine/trade_greeks.py
======================

C6 — Greek drift tracking for open trades.

Computes Black-Scholes delta, vega, and theta for each open trade leg and
aggregates them at trade level. Called daily after EOD bhav data arrives.

Pure functions; all I/O handled by the scheduler job that calls them.
"""

from __future__ import annotations

import math
from datetime import date
from typing import List, Optional, Sequence

from config import STRATEGY_CONFIG

try:
    from scipy.stats import norm
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _norm_cdf(x: float) -> float:
    if _HAS_SCIPY:
        return float(norm.cdf(x))
    # Fallback: Abramowitz & Stegun approximation (error < 7.5×10⁻⁸)
    k = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = k * (0.319381530 + k * (-0.356563782 + k * (1.781477937
           + k * (-1.821255978 + k * 1.330274429))))
    pdf = math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
    q = 1.0 - pdf * poly
    return q if x >= 0 else 1.0 - q


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1d2(
    spot: float,
    strike: float,
    t: float,
    r: float,
    vol: float,
) -> tuple[float, float]:
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * sqrt_t)
    return d1, d1 - vol * sqrt_t


def compute_leg_greeks(
    *,
    spot: float,
    strike: float,
    option_type: str,
    days_to_expiry: int,
    vol: float,
    lots: int,
    lot_size: int,
    action: str,
    risk_free_rate: Optional[float] = None,
) -> dict:
    """Return a dict of {delta, gamma, vega, theta} for one option leg.

    All Greeks are position-adjusted (lot_qty × sign) so they can be summed
    across legs to get the net trade-level Greek.

    Theta is daily (divided by 365 so it represents 1 calendar day of decay).
    Vega is per 1% move in IV (vega / 100).
    """
    r = risk_free_rate if risk_free_rate is not None else STRATEGY_CONFIG.get("risk_free_rate", 0.065)
    t = max(days_to_expiry, 0) / 365.0
    qty = lots * lot_size
    sign = 1.0 if action == "BUY" else -1.0  # short position flips Greeks

    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}

    d1, d2 = _d1d2(spot, strike, t, r, vol)

    if option_type == "CE":
        raw_delta = _norm_cdf(d1)
        raw_theta = (
            -(spot * _norm_pdf(d1) * vol) / (2.0 * math.sqrt(t))
            - r * strike * math.exp(-r * t) * _norm_cdf(d2)
        )
    else:
        raw_delta = _norm_cdf(d1) - 1.0
        raw_theta = (
            -(spot * _norm_pdf(d1) * vol) / (2.0 * math.sqrt(t))
            + r * strike * math.exp(-r * t) * _norm_cdf(-d2)
        )

    raw_gamma = _norm_pdf(d1) / (spot * vol * math.sqrt(t))
    raw_vega  = spot * _norm_pdf(d1) * math.sqrt(t) / 100.0  # per 1% vol move

    return {
        "delta": round(sign * raw_delta * qty, 4),
        "gamma": round(sign * raw_gamma * qty, 6),
        "vega":  round(sign * raw_vega  * qty, 4),
        "theta": round(sign * raw_theta / 365.0 * qty, 4),  # daily theta
    }


def compute_trade_greeks(
    legs: Sequence[dict],
    *,
    spot: float,
    trade_date: date,
) -> dict:
    """Sum leg-level Greeks for an entire trade.

    `legs` should have keys: strike, option_type, action, lots, lot_size,
    expiry_date, and optionally atm_iv (used as vol proxy).

    Returns dict with net delta, gamma, vega, theta and a per-leg breakdown.
    """
    net = {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    breakdown = []
    for leg in legs:
        expiry = leg.get("expiry_date")
        if expiry is None:
            continue
        dte = max((expiry - trade_date).days, 0) if hasattr(expiry, "days") else max(
            (expiry - trade_date).days, 0
        )
        vol = float(leg.get("atm_iv") or leg.get("iv") or 0.20)
        g = compute_leg_greeks(
            spot=spot,
            strike=float(leg["strike"]),
            option_type=str(leg["option_type"]).upper(),
            days_to_expiry=dte,
            vol=vol,
            lots=int(leg.get("lots") or leg.get("lots_actual") or 1),
            lot_size=int(leg.get("lot_size") or 1),
            action=str(leg["action"]).upper(),
        )
        for k in net:
            net[k] = round(net[k] + g[k], 4)
        breakdown.append({
            "leg_order":   leg.get("leg_order"),
            "strike":      float(leg["strike"]),
            "option_type": leg["option_type"],
            "action":      leg["action"],
            **g,
        })

    return {
        "as_of_date": trade_date.isoformat(),
        "net_delta":  net["delta"],
        "net_gamma":  net["gamma"],
        "net_vega":   net["vega"],
        "net_theta":  net["theta"],
        "legs":       breakdown,
    }
