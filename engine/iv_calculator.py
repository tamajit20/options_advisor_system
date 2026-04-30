"""
engine/iv_calculator.py
=======================

Black-Scholes implied volatility via bisection.

Pure functions only. No DB, no I/O.
"""

from __future__ import annotations

import math
from typing import Optional

from scipy.stats import norm

from config import STRATEGY_CONFIG


def _bs_price(spot: float, strike: float, t: float, r: float, vol: float, opt_type: str) -> float:
    """Black-Scholes price for a European option (no dividend)."""
    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        # Intrinsic value at expiry / degenerate input
        if opt_type == "CE":
            return max(spot - strike, 0.0)
        return max(strike - spot, 0.0)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    if opt_type == "CE":
        return spot * norm.cdf(d1) - strike * math.exp(-r * t) * norm.cdf(d2)
    return strike * math.exp(-r * t) * norm.cdf(-d2) - spot * norm.cdf(-d1)


def implied_vol(
    market_price: float,
    spot: float,
    strike: float,
    days_to_expiry: int,
    option_type: str,
    risk_free_rate: Optional[float] = None,
) -> tuple[float, bool]:
    """Compute implied volatility via bisection.

    Returns `(iv, converged)`. On non-convergence returns the best mid-point
    found so far with `converged=False`.
    """
    if market_price <= 0 or spot <= 0 or strike <= 0 or days_to_expiry < 0:
        return 0.0, False

    r = risk_free_rate if risk_free_rate is not None else STRATEGY_CONFIG["risk_free_rate"]
    t = max(days_to_expiry, 0) / 365.0
    if t == 0:
        return 0.0, False

    # Arbitrage bounds — if market price violates them we can't get a valid IV
    if option_type == "CE":
        lower_bound = max(spot - strike * math.exp(-r * t), 0.0)
        upper_bound = spot
    else:
        lower_bound = max(strike * math.exp(-r * t) - spot, 0.0)
        upper_bound = strike * math.exp(-r * t)
    if market_price < lower_bound - 0.01 or market_price > upper_bound + 0.01:
        return 0.0, False

    lo = STRATEGY_CONFIG["iv_bisection_low"]
    hi = STRATEGY_CONFIG["iv_bisection_high"]
    tol = STRATEGY_CONFIG["iv_bisection_tol"]
    max_iter = STRATEGY_CONFIG["iv_bisection_max_iter"]

    p_lo = _bs_price(spot, strike, t, r, lo, option_type) - market_price
    p_hi = _bs_price(spot, strike, t, r, hi, option_type) - market_price
    # If both same sign → no root in interval
    if p_lo * p_hi > 0:
        return (lo if abs(p_lo) < abs(p_hi) else hi), False

    mid = lo
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        p_mid = _bs_price(spot, strike, t, r, mid, option_type) - market_price
        if abs(p_mid) < tol:
            return mid, True
        if p_lo * p_mid < 0:
            hi = mid
            p_hi = p_mid
        else:
            lo = mid
            p_lo = p_mid
    return mid, False


def black_scholes_delta(
    spot: float,
    strike: float,
    days_to_expiry: int,
    vol: float,
    option_type: str,
    risk_free_rate: Optional[float] = None,
) -> float:
    """Black-Scholes delta. CE: 0..1, PE: -1..0."""
    r = risk_free_rate if risk_free_rate is not None else STRATEGY_CONFIG["risk_free_rate"]
    t = max(days_to_expiry, 0) / 365.0
    if t == 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    if option_type == "CE":
        return float(norm.cdf(d1))
    return float(norm.cdf(d1) - 1.0)
