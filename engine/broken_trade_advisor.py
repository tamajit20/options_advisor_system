"""
engine/broken_trade_advisor.py
==============================

When a user reports they couldn't fill all legs of a suggestion (paired hedge
broken, or naked short with no protection), produce a ranked list of
recovery options.

Pure function. Inputs:
    - trade state: list of executed legs + list of un-executed legs from suggestion
    - current chain (for re-pricing)
    - underlying spot

Outputs: List[BrokenTradeOption] sorted by `rank` (1=best).

States handled:
    PAIRED_BROKEN — short leg filled, hedge missing → URGENT close or buy hedge
    NAKED_SHORT   — only short legs filled, no protection at all
    NAKED_LONG    — only long legs filled (lost premium leg) — lower urgency
    PARTIAL_DEBIT — long debit strategy partially built
"""

from __future__ import annotations

from typing import List, Mapping, Sequence

from contracts import BrokenTradeOption


def diagnose(
    executed_legs: Sequence[Mapping],
    not_executed_legs: Sequence[Mapping],
) -> str:
    """Return a state code describing what's wrong."""
    if not not_executed_legs:
        return "FULL"
    if not executed_legs:
        return "NONE_EXECUTED"

    has_short_executed = any(l.get("action") == "SELL" for l in executed_legs)
    has_long_unexec    = any(l.get("action") == "BUY"  for l in not_executed_legs)
    has_long_executed  = any(l.get("action") == "BUY"  for l in executed_legs)
    has_short_unexec   = any(l.get("action") == "SELL" for l in not_executed_legs)

    if has_short_executed and has_long_unexec:
        # Sold premium but hedge missing → most dangerous
        if has_long_executed:
            return "PAIRED_BROKEN"
        return "NAKED_SHORT"
    if has_long_executed and has_short_unexec:
        return "NAKED_LONG"
    return "PARTIAL_DEBIT"


def advise(
    *,
    state: str,
    executed_legs: Sequence[Mapping],
    not_executed_legs: Sequence[Mapping],
    spot: float,
    current_chain: Sequence[Mapping],
) -> List[BrokenTradeOption]:
    """Return ranked recovery options."""
    options: List[BrokenTradeOption] = []

    if state in ("PAIRED_BROKEN", "NAKED_SHORT"):
        options.append(BrokenTradeOption(
            rank=1, label="Exit short legs immediately",
            recommended=True,
            estimated_pnl=_estimate_close_pnl(executed_legs, current_chain),
            when_to_use="Default for unhedged shorts — caps risk",
            zerodha_steps="Place market order to BUY each filled short leg now.",
            time_sensitivity="URGENT",
        ))
        options.append(BrokenTradeOption(
            rank=2, label="Buy missing hedge legs at market",
            recommended=False,
            estimated_pnl=_estimate_complete_pnl(executed_legs, not_executed_legs, current_chain),
            when_to_use="If hedge legs are still affordably priced (slippage < 20%)",
            zerodha_steps="Place limit orders for each missing BUY leg at +5% above mid.",
            time_sensitivity="BEFORE_2PM",
        ))
        options.append(BrokenTradeOption(
            rank=3, label="Roll the short legs further OTM",
            recommended=False,
            estimated_pnl=0.0,
            when_to_use="Only if directional view has strengthened",
            zerodha_steps="Buy back current shorts and sell further OTM strikes same expiry.",
            time_sensitivity="BEFORE_3PM",
        ))
        return options

    if state == "NAKED_LONG":
        options.append(BrokenTradeOption(
            rank=1, label="Hold the long legs",
            recommended=True,
            estimated_pnl=0.0,
            when_to_use="Lost premium-collecting leg but risk is capped at debit paid",
            zerodha_steps="No action — long calls/puts have defined risk.",
            time_sensitivity="LOW",
        ))
        options.append(BrokenTradeOption(
            rank=2, label="Sell the missing short legs to complete the spread",
            recommended=False,
            estimated_pnl=_estimate_complete_pnl(executed_legs, not_executed_legs, current_chain),
            when_to_use="If short premium is still attractive (≥80% of suggested price)",
            zerodha_steps="Place limit SELL orders at suggested price for each missing leg.",
            time_sensitivity="BEFORE_2PM",
        ))
        return options

    if state == "PARTIAL_DEBIT":
        options.append(BrokenTradeOption(
            rank=1, label="Close the executed legs",
            recommended=True,
            estimated_pnl=_estimate_close_pnl(executed_legs, current_chain),
            when_to_use="Avoid open exposure on incomplete debit structure",
            zerodha_steps="Close each executed leg at market.",
            time_sensitivity="BEFORE_3PM",
        ))
        options.append(BrokenTradeOption(
            rank=2, label="Complete the structure",
            recommended=False,
            estimated_pnl=_estimate_complete_pnl(executed_legs, not_executed_legs, current_chain),
            when_to_use="If missing legs are within 10% of suggested prices",
            zerodha_steps="Place limit orders for each missing leg.",
            time_sensitivity="BEFORE_2PM",
        ))
        return options

    # FULL or NONE_EXECUTED — nothing to advise
    return options


def _chain_mid(chain: Sequence[Mapping], strike: float, opt_type: str) -> float:
    for r in chain:
        if float(r["strike"]) == float(strike) and r["option_type"] == opt_type:
            return float(r.get("mid_price") or r.get("settle_price") or r.get("close_price") or 0.0)
    return 0.0


def _estimate_close_pnl(legs: Sequence[Mapping], chain: Sequence[Mapping]) -> float:
    total = 0.0
    for leg in legs:
        mid = _chain_mid(chain, leg["strike"], leg["option_type"])
        qty = int(leg.get("lots") or 0) * int(leg.get("lot_size") or 0)
        fill = float(leg.get("fill_price") or leg.get("suggested_price") or 0.0)
        # If we sold at fill, closing pays mid → PnL = (fill - mid) × qty
        # If we bought at fill, closing receives mid → PnL = (mid - fill) × qty
        if leg.get("action") == "SELL":
            total += (fill - mid) * qty
        else:
            total += (mid - fill) * qty
    return round(total, 2)


def _estimate_complete_pnl(
    executed: Sequence[Mapping],
    pending: Sequence[Mapping],
    chain: Sequence[Mapping],
) -> float:
    """Crude estimate: assume pending legs fill at current mid."""
    total = _estimate_close_pnl(executed, chain) * 0  # baseline 0
    # Net premium of completed structure at current chain
    np_ = 0.0
    qty = 0
    for leg in list(executed) + list(pending):
        mid = _chain_mid(chain, leg["strike"], leg["option_type"])
        q = int(leg.get("lots") or 0) * int(leg.get("lot_size") or 0)
        qty += q
        sign = 1.0 if leg.get("action") == "SELL" else -1.0
        np_ += sign * mid * q
    return round(np_, 2)
