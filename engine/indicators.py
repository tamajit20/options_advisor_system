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
from typing import List, Optional, Sequence, Tuple

from config import STRATEGY_CONFIG
from contracts import ChainTrajectory, MarketIndicators
from engine import trajectory as _traj


# ---------------------------------------------------------------------------
# PCR / Max Pain / OI walls
# ---------------------------------------------------------------------------

def pcr(chain_rows: Sequence[dict]) -> Optional[float]:
    """Put/Call Ratio = ΣPut OI / ΣCall OI for a single expiry.
    Returns None when call OI is absent/zero (OI data not yet published)."""
    call_oi = sum((r.get("open_interest") or 0) for r in chain_rows if r.get("option_type") == "CE")
    put_oi  = sum((r.get("open_interest") or 0) for r in chain_rows if r.get("option_type") == "PE")
    if call_oi <= 0:
        return None
    return put_oi / call_oi


def oi_change_pcr(chain_rows: Sequence[dict]) -> Optional[float]:
    """Put/Call ratio of OI *change* (ΣΔPut OI / ΣΔCall OI).

    Uses the `change_in_oi` field on each row, which is populated as:
    - EOD mode : day-over-day change from the NSE bhav copy (already in the data)
    - Live mode: live_oi − eod_oi computed per strike before calling build_indicators

    Interpretation:
      > 1.2  → puts building faster than calls (bearish pressure, hedging or IV demand)
      0.8–1.2 → balanced OI addition
      < 0.8  → calls building faster (bullish positioning or call writing)
      None   → change_in_oi absent or call delta ≤ 0 (cannot compute ratio)
    """
    rows_with_change = [r for r in chain_rows if r.get("change_in_oi") is not None]
    if not rows_with_change:
        return None
    call_delta = sum((r.get("change_in_oi") or 0) for r in rows_with_change
                     if r.get("option_type") == "CE")
    put_delta  = sum((r.get("change_in_oi") or 0) for r in rows_with_change
                     if r.get("option_type") == "PE")
    if call_delta <= 0:
        return None
    return put_delta / call_delta


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

def atr(spot_history: Sequence[dict], period: int = 14) -> Optional[float]:
    """ATR(period) using Wilder's smoothing on True Range. spot_history
    must be ordered by trade_date asc and contain high_price/low_price/close_price.
    Returns None when fewer than period+1 rows are available."""
    if len(spot_history) < period + 1:
        return None
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
        return None
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def trend(spot_history: Sequence[dict]) -> str:
    """Trend classification using SMA crossover + slope + ADX strength.

    Rules:
        BULLISH  : SMA20 > SMA50 by ``trend_sma_diff_pct`` AND
                   SMA20 5-day slope > ``trend_slope_min_pct`` AND
                   ADX-14 >= ``trend_adx_min``
        BEARISH  : mirror of bullish on the downside
        SIDEWAYS : everything else (chop, weak directional, insufficient data)

    Insufficient history (< 50 closes for SMA50, or ADX unavailable) → SIDEWAYS
    (safe fallback — strategy selector treats sideways conservatively).
    """
    closes = [float(r["close_price"]) for r in spot_history]
    if len(closes) < 50:
        return "SIDEWAYS"
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    if sma50 <= 0:
        return "SIDEWAYS"
    diff_pct = (sma20 - sma50) / sma50 * 100.0

    slope = _sma20_slope_pct(closes)
    adx_val = adx(spot_history, 14)

    sma_min   = STRATEGY_CONFIG.get("trend_sma_diff_pct",   0.5)
    slope_min = STRATEGY_CONFIG.get("trend_slope_min_pct",  0.05)
    adx_min   = STRATEGY_CONFIG.get("trend_adx_min",        20.0)

    # ADX gate: weak trends (< adx_min) collapse to SIDEWAYS even if SMAs diverge.
    # Missing ADX (insufficient history) → conservative SIDEWAYS.
    strong_enough = adx_val is not None and adx_val >= adx_min

    if diff_pct > sma_min and slope is not None and slope > slope_min and strong_enough:
        return "BULLISH"
    if diff_pct < -sma_min and slope is not None and slope < -slope_min and strong_enough:
        return "BEARISH"
    return "SIDEWAYS"


def _sma20_slope_pct(closes: Sequence[float]) -> Optional[float]:
    """SMA20 percentage change over the last 5 days (today's SMA20 vs SMA20 5 days ago).

    Returns None when we don't have 25 closes (need SMA20 today AND 5 days ago).
    """
    if len(closes) < 25:
        return None
    sma_today = sum(closes[-20:]) / 20
    sma_5d_ago = sum(closes[-25:-5]) / 20
    if sma_5d_ago <= 0:
        return None
    return (sma_today - sma_5d_ago) / sma_5d_ago * 100.0


def adx(spot_history: Sequence[dict], period: int = 14) -> Optional[float]:
    """Average Directional Index using Wilder's smoothing.

    Inputs need high_price, low_price, close_price ordered ascending by date.
    Returns None when fewer than 2*period + 1 rows available (need warm-up period
    for both DI smoothing and DX-to-ADX smoothing).
    """
    if len(spot_history) < 2 * period + 1:
        return None

    highs  = [float(r["high_price"])  for r in spot_history]
    lows   = [float(r["low_price"])   for r in spot_history]
    closes = [float(r["close_price"]) for r in spot_history]

    # Compute True Range, +DM, -DM for each bar (i = 1..n-1)
    trs:    List[float] = []
    plus_dm:  List[float] = []
    minus_dm: List[float] = []
    for i in range(1, len(spot_history)):
        up_move   = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move   if (up_move   > down_move and up_move   > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move   and down_move > 0) else 0.0)
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    # Wilder smoothing — initial values = simple sum of first `period`
    atr_w   = sum(trs[:period])
    plus_w  = sum(plus_dm[:period])
    minus_w = sum(minus_dm[:period])

    dxs: List[float] = []
    # First DX from initial smoothed values
    if atr_w > 0:
        plus_di  = 100.0 * plus_w  / atr_w
        minus_di = 100.0 * minus_w / atr_w
        denom = plus_di + minus_di
        if denom > 0:
            dxs.append(100.0 * abs(plus_di - minus_di) / denom)

    # Continue Wilder smoothing through remaining bars
    for i in range(period, len(trs)):
        atr_w   = atr_w   - (atr_w   / period) + trs[i]
        plus_w  = plus_w  - (plus_w  / period) + plus_dm[i]
        minus_w = minus_w - (minus_w / period) + minus_dm[i]
        if atr_w <= 0:
            continue
        plus_di  = 100.0 * plus_w  / atr_w
        minus_di = 100.0 * minus_w / atr_w
        denom = plus_di + minus_di
        if denom <= 0:
            continue
        dxs.append(100.0 * abs(plus_di - minus_di) / denom)

    if len(dxs) < period:
        return None

    # ADX = Wilder-smoothed average of DX values
    adx_val = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return adx_val


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
# Historical Volatility (HV-20)
# ---------------------------------------------------------------------------

def hv_20(spot_history: Sequence[dict]) -> Optional[float]:
    """Annualised 20-day realised volatility (close-to-close log returns).

    Requires at least 22 rows (21 closes → 20 log returns).
    Returns None when insufficient history.
    """
    closes = [float(r["close_price"]) for r in spot_history if r.get("close_price")]
    if len(closes) < 22:
        return None
    recent = closes[-22:]          # last 22 closes → 21 log returns
    log_returns = [
        math.log(recent[i] / recent[i - 1])
        for i in range(1, len(recent))
    ]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    return math.sqrt(variance) * math.sqrt(252)   # annualise


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
    fii_net_futures: Optional[float] = None,
    oi_chain_rows: Optional[Sequence[dict]] = None,
    oi_change_rows: Optional[Sequence[dict]] = None,
    trajectory: Optional[ChainTrajectory] = None,
) -> MarketIndicators:
    # oi_chain_rows: rows to use for absolute OI levels (PCR, max_pain, OI walls).
    # Callers supply yesterday's bhav chain when chain_rows lack open_interest
    # (e.g. Zerodha live-mode falling back to ltp()). chain_rows is never mutated.
    _oi_rows = oi_chain_rows if oi_chain_rows is not None else chain_rows

    # oi_change_rows: rows with change_in_oi for OI momentum (PCR of changes).
    # EOD mode : None → fall back to chain_rows which has change_in_oi from bhav.
    # Live mode: caller supplies pre-computed delta rows (live_oi − eod_oi/strike).
    _change_rows = oi_change_rows if oi_change_rows is not None else chain_rows

    cw, pw = oi_walls(_oi_rows)
    hv = hv_20(spot_history)
    iv_prem = (atm_iv / hv) if (hv is not None and hv > 0) else None

    # Phase 1: trend strength + slope diagnostics
    closes = [float(r["close_price"]) for r in spot_history]
    sma_diff = None
    if len(closes) >= 50:
        sma20_v = sum(closes[-20:]) / 20
        sma50_v = sum(closes[-50:]) / 50
        if sma50_v > 0:
            sma_diff = (sma20_v - sma50_v) / sma50_v * 100.0
    slope_v = _sma20_slope_pct(closes)
    adx_v   = adx(spot_history, 14)

    # ── Trajectory-derived fields (live mode only) ────────────────────────
    # All None unless a ChainTrajectory bundle is supplied with enough samples.
    oi_pcr_slope = oi_pcr_persist = None
    iv_slope = iv_persist = None
    call_spr_bps = put_spr_bps = None
    vol_burst = None
    if trajectory is not None:
        min_n = STRATEGY_CONFIG.get("trajectory_min_samples", 3)
        if len(_traj._clean(trajectory.oi_pcr_change_series)) >= min_n:
            oi_pcr_slope   = _traj.slope_pct(trajectory.oi_pcr_change_series)
            oi_pcr_persist = _traj.persistence(trajectory.oi_pcr_change_series)
        if len(_traj._clean(trajectory.atm_iv_series)) >= min_n:
            iv_slope   = _traj.slope_pct(trajectory.atm_iv_series)
            iv_persist = _traj.persistence(trajectory.atm_iv_series)
        call_spr_bps = trajectory.latest_call_spread_bps
        put_spr_bps  = trajectory.latest_put_spread_bps
        # Volume burst z-score: last bucket vs trailing mean (call+put combined).
        combined_vol: List[Optional[float]] = []
        cv = trajectory.call_volume_series or []
        pv = trajectory.put_volume_series or []
        for i in range(max(len(cv), len(pv))):
            c = cv[i] if i < len(cv) else None
            p = pv[i] if i < len(pv) else None
            if c is None and p is None:
                combined_vol.append(None)
            else:
                combined_vol.append((c or 0.0) + (p or 0.0))
        cleaned = _traj._clean(combined_vol)
        if len(cleaned) >= 4:
            last = cleaned[-1]
            prior = cleaned[:-1]
            mean = sum(prior) / len(prior)
            var = sum((x - mean) ** 2 for x in prior) / len(prior)
            std = math.sqrt(var) if var > 0 else 0.0
            if std > 0:
                vol_burst = (last - mean) / std

    return MarketIndicators(
        symbol           = symbol,
        as_of            = as_of,
        spot             = spot,
        pcr              = pcr(_oi_rows),
        max_pain         = max_pain(_oi_rows),
        atr_14           = atr(spot_history, 14),
        trend            = trend(spot_history),
        vix_close        = float(vix_history[-1]["close_price"]) if vix_history else None,
        vix_regime       = vix_regime(vix_history),
        oi_walls_call    = cw,
        oi_walls_put     = pw,
        expected_move    = expected_move(spot, atm_iv, dte),
        hv_20            = hv,
        iv_premium       = iv_prem,
        fii_net_futures  = fii_net_futures,
        adx_14           = adx_v,
        sma20_slope_pct  = slope_v,
        sma_diff_pct     = sma_diff,
        oi_pcr_change    = oi_change_pcr(_change_rows),
        oi_pcr_slope_5min   = oi_pcr_slope,
        oi_pcr_persistence  = oi_pcr_persist,
        atm_iv_slope_5min   = iv_slope,
        atm_iv_persistence  = iv_persist,
        atm_call_spread_bps = call_spr_bps,
        atm_put_spread_bps  = put_spr_bps,
        volume_burst_z      = vol_burst,
    )
