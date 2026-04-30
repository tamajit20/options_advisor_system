"""
engine/indicators.py
====================

Pure functions to compute market indicators from raw chain + spot history.

Inputs are plain dicts/lists (typically as fetched by the database repos);
outputs are `MarketIndicators` from contracts.

NO DB / I/O — all data must be passed in.
"""

from __future__ import annotations

import math
from datetime import date
from typing import List, Sequence, Tuple

from config import STRATEGY_CONFIG
from contracts import MarketIndicators


# ---------------------------------------------------------------------------
# PCR / Max Pain / OI walls
# ---------------------------------------------------------------------------

def pcr(chain_rows: Sequence[dict]) -> float:
    """Put/Call Ratio = ΣPut OI / ΣCall OI for a single expiry."""
    call_oi = sum((r.get("open_interest") or 0) for r in chain_rows if r.get("option_type") == "CE")
    put_oi  = sum((r.get("open_interest") or 0) for r in chain_rows if r.get("option_type") == "PE")
    if call_oi <= 0:
        return 0.0
    return put_oi / call_oi


def max_pain(chain_rows: Sequence[dict]) -> float:
    """Strike where total option-buyer payout is minimum at expiry."""
    if not chain_rows:
        return 0.0
    strikes = sorted({float(r["strike"]) for r in chain_rows})
    if not strikes:
        return 0.0
    by_strike: dict[tuple[float, str], int] = {}
    for r in chain_rows:
        k = (float(r["strike"]), r["option_type"])
        by_strike[k] = by_strike.get(k, 0) + (r.get("open_interest") or 0)

    best_strike = strikes[0]
    best_payout = float("inf")
    for s in strikes:
        total = 0.0
        for k in strikes:
            ce_oi = by_strike.get((k, "CE"), 0)
            pe_oi = by_strike.get((k, "PE"), 0)
            # Payout to option buyers if expiry settles at s
            total += max(s - k, 0.0) * ce_oi
            total += max(k - s, 0.0) * pe_oi
        if total < best_payout:
            best_payout = total
            best_strike = s
    return best_strike


def oi_walls(chain_rows: Sequence[dict], top_n: int = 3) -> Tuple[List[float], List[float]]:
    """Return (top_call_walls, top_put_walls) by absolute OI."""
    calls = [(float(r["strike"]), r.get("open_interest") or 0)
             for r in chain_rows if r.get("option_type") == "CE"]
    puts  = [(float(r["strike"]), r.get("open_interest") or 0)
             for r in chain_rows if r.get("option_type") == "PE"]
    calls.sort(key=lambda x: -x[1])
    puts.sort(key=lambda x: -x[1])
    return [s for s, _ in calls[:top_n]], [s for s, _ in puts[:top_n]]


# ---------------------------------------------------------------------------
# Spot-based indicators
# ---------------------------------------------------------------------------

def atr(spot_history: Sequence[dict], period: int = 14) -> float:
    """ATR(period) using Wilder's smoothing on True Range. spot_history
    must be ordered by trade_date asc and contain high_price/low_price/close_price."""
    if len(spot_history) < period + 1:
        return 0.0
    trs: List[float] = []
    prev_close = float(spot_history[0]["close_price"])
    for r in spot_history[1:]:
        h = float(r["high_price"])
        l = float(r["low_price"])
        c = float(r["close_price"])
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    if len(trs) < period:
        return 0.0
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def trend(spot_history: Sequence[dict]) -> str:
    """SMA20 vs SMA50 → BULLISH / BEARISH / SIDEWAYS."""
    closes = [float(r["close_price"]) for r in spot_history]
    if len(closes) < 50:
        return "SIDEWAYS"
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    diff = (sma20 - sma50) / sma50 * 100.0
    if diff > 0.5:
        return "BULLISH"
    if diff < -0.5:
        return "BEARISH"
    return "SIDEWAYS"


# ---------------------------------------------------------------------------
# VIX regime
# ---------------------------------------------------------------------------

def vix_regime(vix_history: Sequence[dict]) -> str:
    """STABLE / RISING / SPIKING based on % change vs prior close."""
    if len(vix_history) < 2:
        return "STABLE"
    today = float(vix_history[-1]["close_price"])
    prev  = float(vix_history[-2]["close_price"])
    if prev <= 0:
        return "STABLE"
    pct = (today - prev) / prev * 100.0
    if pct >= STRATEGY_CONFIG["vix_spiking_threshold"]:
        return "SPIKING"
    if pct >= STRATEGY_CONFIG["vix_rising_threshold"]:
        return "RISING"
    return "STABLE"


# ---------------------------------------------------------------------------
# Expected move
# ---------------------------------------------------------------------------

def expected_move(spot: float, atm_iv: float, dte: int) -> float:
    if spot <= 0 or atm_iv <= 0 or dte <= 0:
        return 0.0
    return spot * atm_iv * math.sqrt(dte / 365.0)


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def build_indicators(
    *,
    symbol: str,
    as_of: date,
    spot: float,
    chain_rows: Sequence[dict],
    spot_history: Sequence[dict],
    vix_history: Sequence[dict],
    atm_iv: float,
    dte: int,
) -> MarketIndicators:
    cw, pw = oi_walls(chain_rows)
    return MarketIndicators(
        symbol        = symbol,
        as_of         = as_of,
        spot          = spot,
        pcr           = pcr(chain_rows),
        max_pain      = max_pain(chain_rows),
        atr_14        = atr(spot_history, 14),
        trend         = trend(spot_history),
        vix_close     = float(vix_history[-1]["close_price"]) if vix_history else 0.0,
        vix_regime    = vix_regime(vix_history),
        oi_walls_call = cw,
        oi_walls_put  = pw,
        expected_move = expected_move(spot, atm_iv, dte),
    )
