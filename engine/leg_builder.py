"""
engine/leg_builder.py
=====================

Helpers to construct strategy legs from a chain.

Pure functions. Inputs:
    - `chain`: list of dicts (rows from options_fo_eod) for ONE expiry
    - `spot`, `dte`, `expected_move`, lot_size, atm_iv

Outputs: lists of `SuggestionLeg` plus economics primitives.
"""

from __future__ import annotations

import math
from datetime import date
from typing import List, Mapping, Optional, Sequence

from contracts import SuggestionLeg
from engine.iv_calculator import black_scholes_delta


# ---------------------------------------------------------------------------
# Strike helpers
# ---------------------------------------------------------------------------

def closest_strike(strikes: Sequence[float], target: float) -> float:
    if not strikes:
        raise ValueError("No strikes available")
    return min(strikes, key=lambda s: abs(s - target))


def get_chain_row(chain: Sequence[Mapping], strike: float, option_type: str) -> Optional[Mapping]:
    for r in chain:
        if float(r["strike"]) == float(strike) and r["option_type"] == option_type:
            return r
    return None


def mid_price(row: Mapping) -> float:
    """Use settle price if available; fallback to close."""
    sp = float(row.get("settle_price") or 0.0)
    if sp > 0:
        return sp
    return float(row.get("close_price") or 0.0)


def price_band(row: Mapping, band_pct: float = 0.02) -> tuple[float, float]:
    p = mid_price(row)
    if p <= 0:
        return 0.0, 0.0
    return round(p * (1 - band_pct), 2), round(p * (1 + band_pct), 2)


# ---------------------------------------------------------------------------
# Probability of profit (PoP) from delta
# ---------------------------------------------------------------------------

def pop_from_delta(delta: float, side: str) -> float:
    """For a SHORT option, PoP = 1 − |delta|. For a LONG it's |delta|.
    Returns percent 0..100."""
    d = abs(delta)
    if side.upper() == "SELL":
        return max(0.0, min(100.0, (1 - d) * 100.0))
    return max(0.0, min(100.0, d * 100.0))


# ---------------------------------------------------------------------------
# Strategy-specific leg builders
# ---------------------------------------------------------------------------

def build_iron_condor(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    expected_move: float,
    lots: int,
    lot_size: int,
    wing_width: Optional[float] = None,
) -> List[SuggestionLeg]:
    """4-leg iron condor.

    Short legs at ~1σ from spot (uses expected_move), long wings further out.
    """
    if not chain or expected_move <= 0:
        raise ValueError("Insufficient chain/EM for condor")

    strikes = sorted({float(r["strike"]) for r in chain})
    if not strikes:
        raise ValueError("No strikes in chain")
    step = _strike_step(strikes)
    if wing_width is None:
        wing_width = max(step * 2, expected_move * 0.5)

    short_call_target = spot + expected_move
    short_put_target  = spot - expected_move
    short_call = closest_strike(strikes, short_call_target)
    short_put  = closest_strike(strikes, short_put_target)
    long_call  = closest_strike(strikes, short_call + wing_width)
    long_put   = closest_strike(strikes, short_put  - wing_width)

    legs: List[SuggestionLeg] = []
    legs.append(_make_leg(1, 2, underlying, expiry, short_put,  "PE", "SELL", lots, lot_size, chain,
                          "Short put — collects premium below expected move"))
    legs.append(_make_leg(2, 1, underlying, expiry, long_put,   "PE", "BUY",  lots, lot_size, chain,
                          "Long put hedge — caps downside risk"))
    legs.append(_make_leg(3, 4, underlying, expiry, short_call, "CE", "SELL", lots, lot_size, chain,
                          "Short call — collects premium above expected move"))
    legs.append(_make_leg(4, 3, underlying, expiry, long_call,  "CE", "BUY",  lots, lot_size, chain,
                          "Long call hedge — caps upside risk"))
    return legs


def build_bull_put_spread(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    expected_move: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    strikes = sorted({float(r["strike"]) for r in chain})
    step = _strike_step(strikes)
    short_put = closest_strike(strikes, spot - expected_move)
    long_put  = closest_strike(strikes, short_put - max(step * 2, expected_move * 0.5))
    legs = [
        _make_leg(1, 2, underlying, expiry, short_put, "PE", "SELL", lots, lot_size, chain,
                  "Short put — bullish premium"),
        _make_leg(2, 1, underlying, expiry, long_put,  "PE", "BUY",  lots, lot_size, chain,
                  "Long put — defines max loss"),
    ]
    return legs


def build_bear_call_spread(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    expected_move: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    strikes = sorted({float(r["strike"]) for r in chain})
    step = _strike_step(strikes)
    short_call = closest_strike(strikes, spot + expected_move)
    long_call  = closest_strike(strikes, short_call + max(step * 2, expected_move * 0.5))
    legs = [
        _make_leg(1, 2, underlying, expiry, short_call, "CE", "SELL", lots, lot_size, chain,
                  "Short call — bearish premium"),
        _make_leg(2, 1, underlying, expiry, long_call,  "CE", "BUY",  lots, lot_size, chain,
                  "Long call — defines max loss"),
    ]
    return legs


def build_long_straddle(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    strikes = sorted({float(r["strike"]) for r in chain})
    atm = closest_strike(strikes, spot)
    return [
        _make_leg(1, None, underlying, expiry, atm, "CE", "BUY", lots, lot_size, chain,
                  "Long ATM call — directional / vol-buying"),
        _make_leg(2, None, underlying, expiry, atm, "PE", "BUY", lots, lot_size, chain,
                  "Long ATM put — directional / vol-buying"),
    ]


def build_long_strangle(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    expected_move: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    strikes = sorted({float(r["strike"]) for r in chain})
    long_call = closest_strike(strikes, spot + expected_move * 0.5)
    long_put  = closest_strike(strikes, spot - expected_move * 0.5)
    return [
        _make_leg(1, None, underlying, expiry, long_call, "CE", "BUY", lots, lot_size, chain,
                  "Long OTM call — vol-buying upside"),
        _make_leg(2, None, underlying, expiry, long_put,  "PE", "BUY", lots, lot_size, chain,
                  "Long OTM put — vol-buying downside"),
    ]


def build_long_call(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    """Single-leg long ATM call — strong bullish, low IV."""
    strikes = sorted({float(r["strike"]) for r in chain})
    atm = closest_strike(strikes, spot)
    return [
        _make_leg(1, None, underlying, expiry, atm, "CE", "BUY", lots, lot_size, chain,
                  "Long ATM call — directional bullish, unlimited upside"),
    ]


def build_long_put(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    """Single-leg long ATM put — strong bearish, low IV."""
    strikes = sorted({float(r["strike"]) for r in chain})
    atm = closest_strike(strikes, spot)
    return [
        _make_leg(1, None, underlying, expiry, atm, "PE", "BUY", lots, lot_size, chain,
                  "Long ATM put — directional bearish, defined max loss = premium"),
    ]


def build_bull_call_spread(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    expected_move: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    """Debit bull call spread — buy ATM/slightly-OTM call, sell further OTM call."""
    strikes = sorted({float(r["strike"]) for r in chain})
    step = _strike_step(strikes)
    long_call  = closest_strike(strikes, spot)
    short_call = closest_strike(strikes, long_call + max(step * 2, expected_move * 0.5))
    return [
        _make_leg(1, 2, underlying, expiry, long_call,  "CE", "BUY",  lots, lot_size, chain,
                  "Long call — bullish debit"),
        _make_leg(2, 1, underlying, expiry, short_call, "CE", "SELL", lots, lot_size, chain,
                  "Short call — caps upside, reduces cost"),
    ]


def build_bear_put_spread(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    expected_move: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    """Debit bear put spread — buy ATM/slightly-OTM put, sell further OTM put."""
    strikes = sorted({float(r["strike"]) for r in chain})
    step = _strike_step(strikes)
    long_put  = closest_strike(strikes, spot)
    short_put = closest_strike(strikes, long_put - max(step * 2, expected_move * 0.5))
    return [
        _make_leg(1, 2, underlying, expiry, long_put,  "PE", "BUY",  lots, lot_size, chain,
                  "Long put — bearish debit"),
        _make_leg(2, 1, underlying, expiry, short_put, "PE", "SELL", lots, lot_size, chain,
                  "Short put — caps downside profit, reduces cost"),
    ]


def build_iron_butterfly(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    expected_move: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    """4-leg iron butterfly — short straddle ATM + long wings at ±EM. High premium, narrow range."""
    if not chain:
        raise ValueError("Empty chain")
    strikes = sorted({float(r["strike"]) for r in chain})
    atm = closest_strike(strikes, spot)
    step = _strike_step(strikes)
    wing_width = max(step * 2, expected_move)
    long_call = closest_strike(strikes, atm + wing_width)
    long_put  = closest_strike(strikes, atm - wing_width)
    return [
        _make_leg(1, 2, underlying, expiry, atm,       "PE", "SELL", lots, lot_size, chain,
                  "Short ATM put — body of butterfly"),
        _make_leg(2, 1, underlying, expiry, long_put,  "PE", "BUY",  lots, lot_size, chain,
                  "Long OTM put — caps downside"),
        _make_leg(3, 4, underlying, expiry, atm,       "CE", "SELL", lots, lot_size, chain,
                  "Short ATM call — body of butterfly"),
        _make_leg(4, 3, underlying, expiry, long_call, "CE", "BUY",  lots, lot_size, chain,
                  "Long OTM call — caps upside"),
    ]


def build_jade_lizard(
    *,
    underlying: str,
    expiry: date,
    chain: Sequence[Mapping],
    spot: float,
    expected_move: float,
    lots: int,
    lot_size: int,
) -> List[SuggestionLeg]:
    """3-leg jade lizard — short put + short call spread. Net credit ≥ call-spread width
    eliminates upside risk; downside risk = (short put strike − net credit)."""
    strikes = sorted({float(r["strike"]) for r in chain})
    step = _strike_step(strikes)
    short_put  = closest_strike(strikes, spot - expected_move)
    short_call = closest_strike(strikes, spot + expected_move * 0.5)
    long_call  = closest_strike(strikes, short_call + max(step * 2, expected_move * 0.5))
    return [
        _make_leg(1, None, underlying, expiry, short_put,  "PE", "SELL", lots, lot_size, chain,
                  "Short OTM put — bullish premium"),
        _make_leg(2, 3,    underlying, expiry, short_call, "CE", "SELL", lots, lot_size, chain,
                  "Short OTM call — premium leg of upside spread"),
        _make_leg(3, 2,    underlying, expiry, long_call,  "CE", "BUY",  lots, lot_size, chain,
                  "Long further-OTM call — caps upside risk"),
    ]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strike_step(strikes: Sequence[float]) -> float:
    """Estimate the strike interval (e.g., 50 for NIFTY weekly)."""
    if len(strikes) < 2:
        return 50.0
    diffs = sorted(round(strikes[i + 1] - strikes[i], 4) for i in range(len(strikes) - 1))
    # mode-ish: take median of positive diffs
    pos = [d for d in diffs if d > 0]
    if not pos:
        return 50.0
    return pos[len(pos) // 2]


def _make_leg(
    leg_order: int,
    pair: Optional[int],
    underlying: str,
    expiry: date,
    strike: float,
    option_type: str,
    action: str,
    lots: int,
    lot_size: int,
    chain: Sequence[Mapping],
    note: str,
) -> SuggestionLeg:
    row = get_chain_row(chain, strike, option_type)
    if row is None:
        raise ValueError(f"Strike {strike} {option_type} missing from chain")
    p = mid_price(row)
    lo, hi = price_band(row)
    return SuggestionLeg(
        leg_order=leg_order,
        hedge_pair_leg=pair,
        symbol=underlying,
        expiry_date=expiry,
        strike=float(strike),
        option_type=option_type,
        action=action,
        lots=lots,
        lot_size=lot_size,
        suggested_price=round(p, 2),
        suggested_price_low=lo,
        suggested_price_high=hi,
        leg_purpose_note=note,
    )


# ---------------------------------------------------------------------------
# Economics: breakevens + max profit/loss + PoP
# ---------------------------------------------------------------------------

def net_premium(legs: Sequence[SuggestionLeg]) -> float:
    """Positive = net credit, negative = net debit (per share, before charges)."""
    total = 0.0
    for leg in legs:
        sign = 1.0 if leg.action == "SELL" else -1.0
        total += sign * leg.suggested_price
    return total


def spread_width(legs: Sequence[SuggestionLeg]) -> float:
    """Maximum width across paired hedge legs of the same option type+action axis."""
    if not legs:
        return 0.0
    # Group by option_type
    widths: List[float] = []
    by_type: dict[str, list[SuggestionLeg]] = {"CE": [], "PE": []}
    for leg in legs:
        by_type[leg.option_type].append(leg)
    for opts in by_type.values():
        if not opts:
            continue
        sells = [l.strike for l in opts if l.action == "SELL"]
        buys  = [l.strike for l in opts if l.action == "BUY"]
        if sells and buys:
            widths.append(abs(buys[0] - sells[0]))
    return max(widths) if widths else 0.0


def estimate_pop(legs: Sequence[SuggestionLeg], spot: float, dte: int, atm_iv: float) -> float:
    """Probability of profit ≈ 1 − |Δ_short_leg| (averaged across short legs)."""
    short_legs = [l for l in legs if l.action == "SELL"]
    if not short_legs:
        # Long-premium strategy — use long-leg delta as a rough estimate
        long_legs = [l for l in legs if l.action == "BUY"]
        if not long_legs:
            return 50.0
        deltas = [abs(black_scholes_delta(spot, l.strike, dte, atm_iv, l.option_type)) for l in long_legs]
        return 100.0 * sum(deltas) / len(deltas)
    deltas = [abs(black_scholes_delta(spot, l.strike, dte, atm_iv, l.option_type)) for l in short_legs]
    avg = sum(deltas) / len(deltas)
    return max(0.0, min(100.0, (1 - avg) * 100.0))


def breakevens(legs: Sequence[SuggestionLeg], strategy: str) -> tuple[Optional[float], Optional[float]]:
    """Compute upper/lower breakevens for common defined-risk structures."""
    np_ = net_premium(legs)
    by_type: dict[str, list[SuggestionLeg]] = {"CE": [], "PE": []}
    for leg in legs:
        by_type[leg.option_type].append(leg)

    if strategy in ("IRON_CONDOR",):
        short_call = next((l.strike for l in by_type["CE"] if l.action == "SELL"), None)
        short_put  = next((l.strike for l in by_type["PE"] if l.action == "SELL"), None)
        if short_call is None or short_put is None:
            return None, None
        upper = short_call + np_
        lower = short_put  - np_
        return upper, lower

    if strategy == "BULL_PUT_SPREAD":
        short_put = next((l.strike for l in by_type["PE"] if l.action == "SELL"), None)
        if short_put is None:
            return None, None
        return None, short_put - np_

    if strategy == "BEAR_CALL_SPREAD":
        short_call = next((l.strike for l in by_type["CE"] if l.action == "SELL"), None)
        if short_call is None:
            return None, None
        return short_call + np_, None

    if strategy == "LONG_STRADDLE":
        atm = legs[0].strike if legs else 0.0
        debit = -np_
        return atm + debit, atm - debit

    if strategy == "LONG_STRANGLE":
        long_call = next((l.strike for l in by_type["CE"] if l.action == "BUY"), None)
        long_put  = next((l.strike for l in by_type["PE"] if l.action == "BUY"), None)
        debit = -np_
        upper = (long_call + debit) if long_call else None
        lower = (long_put - debit) if long_put else None
        return upper, lower

    if strategy == "LONG_CALL":
        long_call = next((l.strike for l in by_type["CE"] if l.action == "BUY"), None)
        debit = -np_
        return (long_call + debit if long_call else None), None

    if strategy == "LONG_PUT":
        long_put = next((l.strike for l in by_type["PE"] if l.action == "BUY"), None)
        debit = -np_
        return None, (long_put - debit if long_put else None)

    if strategy == "BULL_CALL_SPREAD":
        long_call = next((l.strike for l in by_type["CE"] if l.action == "BUY"), None)
        debit = -np_
        return (long_call + debit if long_call else None), None

    if strategy == "BEAR_PUT_SPREAD":
        long_put = next((l.strike for l in by_type["PE"] if l.action == "BUY"), None)
        debit = -np_
        return None, (long_put - debit if long_put else None)

    if strategy == "IRON_BUTTERFLY":
        atm = next((l.strike for l in by_type["CE"] if l.action == "SELL"), None)
        if atm is None:
            return None, None
        return atm + np_, atm - np_

    if strategy == "JADE_LIZARD":
        short_put  = next((l.strike for l in by_type["PE"] if l.action == "SELL"), None)
        short_call = next((l.strike for l in by_type["CE"] if l.action == "SELL"), None)
        if short_put is None or short_call is None:
            return None, None
        # Upper BE only meaningful if net credit < call-spread width
        long_call = next((l.strike for l in by_type["CE"] if l.action == "BUY"), None)
        call_width = (long_call - short_call) if long_call else 0.0
        upper = (short_call + np_) if np_ < call_width else None  # None = no upside risk
        lower = short_put - np_
        return upper, lower

    return None, None


def max_profit_loss(legs: Sequence[SuggestionLeg], strategy: str) -> tuple[float, float]:
    """Return (max_profit_per_share, max_loss_per_share). max_loss is positive."""
    np_ = net_premium(legs)
    width = spread_width(legs)
    if strategy in ("IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "IRON_BUTTERFLY"):
        # Defined-risk credit: max profit = net credit, max loss = width − credit
        return max(np_, 0.0), max(width - np_, 0.0)
    if strategy in ("BULL_CALL_SPREAD", "BEAR_PUT_SPREAD"):
        # Defined-risk debit: max loss = debit, max profit = width − debit
        debit = max(-np_, 0.0)
        return max(width - debit, 0.0), debit
    if strategy in ("LONG_STRADDLE", "LONG_STRANGLE", "LONG_CALL", "LONG_PUT"):
        # Long premium: max loss = debit, max profit = unbounded
        debit = max(-np_, 0.0)
        return float("inf"), debit
    if strategy == "JADE_LIZARD":
        # Max profit = net credit (price stays between short strikes).
        # Max loss = max(short_put − net_credit, call_spread_width − net_credit).
        # If credit ≥ call_spread_width, upside risk = 0, so loss is purely downside.
        by_type: dict[str, list[SuggestionLeg]] = {"CE": [], "PE": []}
        for leg in legs:
            by_type[leg.option_type].append(leg)
        short_put  = next((l.strike for l in by_type["PE"] if l.action == "SELL"), None)
        short_call = next((l.strike for l in by_type["CE"] if l.action == "SELL"), None)
        long_call  = next((l.strike for l in by_type["CE"] if l.action == "BUY"), None)
        if short_put is None:
            return 0.0, 0.0
        call_width = (long_call - short_call) if (long_call and short_call) else 0.0
        downside_loss = max(short_put - np_, 0.0)   # if assigned at expiry below short_put
        upside_loss   = max(call_width - np_, 0.0)
        return max(np_, 0.0), max(downside_loss, upside_loss)
    return 0.0, 0.0
