"""
engine/exit_pricing.py
======================

Shared helpers for computing realistic option close prices when settling /
closing a trade. Sanitises raw EOD chain rows that occasionally carry a
spot-value-shaped ``settle_price`` (observed in prod on NIFTY 24300 PE where
``settle_price`` came back as ~23,618, equal to NIFTY spot) so that downstream
P&L estimates do not balloon by 3-4 orders of magnitude.

Both the batch EOD ``lifecycle.exit_orchestrator`` and the interactive
``dashboard.server`` close-suggestion endpoint go through this module.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence, Tuple


def intrinsic_value(option_type: str, strike: float, spot: float) -> float:
    """Cash-settlement value of an option at expiry.

    CE = max(0, spot - strike); PE = max(0, strike - spot).
    """
    if (option_type or "").upper() == "CE":
        return max(0.0, float(spot) - float(strike))
    return max(0.0, float(strike) - float(spot))


def sanitized_close_price(
    *,
    option_type: str,
    strike: float,
    raw_mid: float,
    spot: Optional[float],
) -> Tuple[float, str]:
    """Return a defensible close-price for the leg, plus a source tag.

    Sanity rule: an option premium has a theoretical upper bound (CE ≤ spot,
    PE ≤ strike) but in practice even deep-ITM index options rarely exceed
    ~30% of the underlying value, and an ATM short-dated option is closer to
    5-10%. We flag anything above 50% of ``max(strike, spot)`` as bogus —
    this catches the production bug where ``settle_price`` was being written
    as the spot value — while still permitting any realistic premium
    including deep ITM.

    Parameters
    ----------
    option_type:
        ``"CE"`` or ``"PE"``.
    strike:
        Leg strike (positive number).
    raw_mid:
        The raw close / settle price as read from the EOD row.
    spot:
        Underlying close on the same trade date. When ``None`` we cannot
        compute intrinsic and the raw value is passed through untouched.

    Returns
    -------
    (price, source) where ``source`` is one of ``"mid"`` (raw value used) or
    ``"intrinsic_fallback"`` (raw flagged as bogus → replaced with intrinsic).
    """
    try:
        mid = float(raw_mid or 0.0)
    except (TypeError, ValueError):
        mid = 0.0
    if spot is None:
        return mid, "mid"
    upper_cap = max(float(strike), float(spot)) * 0.5
    if mid > upper_cap or mid < 0.0:
        return intrinsic_value(option_type, float(strike), float(spot)), "intrinsic_fallback"
    return mid, "mid"


def expiry_iso(raw: Any) -> str:
    """Normalise an expiry to ``YYYY-MM-DD`` (empty string if missing)."""
    if raw is None:
        return ""
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    text = str(raw).strip()
    return text[:10] if text else ""


def expiry_date(raw: Any) -> Optional[date]:
    iso = expiry_iso(raw)
    if not iso:
        return None
    try:
        return date.fromisoformat(iso)
    except ValueError:
        return None


def format_leg_quote_key(
    symbol: Any,
    expiry: Any,
    strike: Any,
    option_type: Any,
) -> str:
    """SSE / snapshot key: ``SYMBOL|YYYY-MM-DD|strike|CE``.

    Expiry is required so calendar legs (same strike + CE/PE, different
    expiry) do not collapse onto one live price.
    """
    return (
        f"{str(symbol or '').upper()}|"
        f"{expiry_iso(expiry)}|"
        f"{float(strike)}|"
        f"{str(option_type or '').upper()}"
    )


def _legacy_leg_quote_key(symbol: Any, strike: Any, option_type: Any) -> str:
    """Pre-expiry snapshot key: ``SYMBOL|strike|CE``."""
    return (
        f"{str(symbol or '').upper()}|"
        f"{float(strike)}|"
        f"{str(option_type or '').upper()}"
    )


def lookup_leg_ltp(
    leg_ltps: Optional[Mapping[str, Any]],
    *,
    symbol: Any,
    strike: Any,
    option_type: Any,
    expiry: Any = None,
) -> Optional[float]:
    """Resolve a live mark from a snapshot map. Mirrors dashboard ``_lookupLegLtp``.

    Prefer the 4-part expiry key. Fall back to a legacy 3-part key only when
    exactly one match exists — two calendar CEs at the same strike must not
    share a price.
    """
    if not leg_ltps:
        return None
    exp = expiry_iso(expiry)
    if exp:
        want = format_leg_quote_key(symbol, exp, strike, option_type)
        for key, value in leg_ltps.items():
            parts = str(key).split("|")
            if len(parts) != 4:
                continue
            if format_leg_quote_key(parts[0], parts[1], parts[2], parts[3]) == want:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
    want3 = _legacy_leg_quote_key(symbol, strike, option_type)
    matches: list[float] = []
    for key, value in leg_ltps.items():
        parts = str(key).split("|")
        if len(parts) != 3:
            continue
        if _legacy_leg_quote_key(parts[0], parts[1], parts[2]) != want3:
            continue
        try:
            matches.append(float(value))
        except (TypeError, ValueError):
            continue
    if len(matches) == 1:
        return matches[0]
    return None


def leg_close_pnl(
    *,
    action: str,
    fill_price: float,
    close_price: float,
    lots: int,
    lot_size: int,
) -> float:
    """Gross P&L to close one leg. Same sign convention as live MTM.

    SELL (buy-to-close): (entry − close) × qty.
    BUY (sell-to-close): (close − entry) × qty.
    """
    qty = int(lots or 0) * int(lot_size or 0)
    fill = float(fill_price or 0.0)
    close = float(close_price or 0.0)
    if str(action or "").upper() == "SELL":
        return (fill - close) * qty
    return (close - fill) * qty


def mid_from_chain_rows(
    chain_rows: Sequence[Mapping],
    strike: Any,
    option_type: Any,
) -> float:
    """Look up settle/close/mid for one (strike, CE/PE) within a single expiry chain."""
    strike_f = float(strike)
    ot = str(option_type or "").upper()
    for row in chain_rows or []:
        if float(row["strike"]) != strike_f:
            continue
        if str(row.get("option_type") or "").upper() != ot:
            continue
        if row.get("mid_price") is not None:
            return float(row["mid_price"])
        return float(row.get("settle_price") or row.get("close_price") or 0.0)
    return 0.0


def _chain_for_expiry(
    chains_by_expiry: Mapping[Any, Sequence[Mapping]],
    expiry: Any,
) -> Sequence[Mapping]:
    if not chains_by_expiry:
        return []
    if expiry in chains_by_expiry:
        return chains_by_expiry[expiry]
    iso = expiry_iso(expiry)
    if iso and iso in chains_by_expiry:
        return chains_by_expiry[iso]
    as_date = expiry_date(expiry)
    if as_date is not None and as_date in chains_by_expiry:
        return chains_by_expiry[as_date]
    return []


def aligned_current_chain(
    legs: Sequence[Mapping],
    chains_by_expiry: Mapping[Any, Sequence[Mapping]],
) -> list[dict]:
    """One ``current_chain`` row per leg, using that leg's own expiry.

    ``evaluate_exit`` matches mids positionally when lengths match, so this
    is what keeps calendar (same strike, two expiries) from collapsing.
    """
    out: list[dict] = []
    for leg in legs:
        exp = expiry_iso(leg.get("expiry_date") or leg.get("expiry"))
        chain = _chain_for_expiry(chains_by_expiry, leg.get("expiry_date") or exp)
        mid = mid_from_chain_rows(chain, leg["strike"], leg["option_type"])
        out.append({
            "strike": float(leg["strike"]),
            "option_type": leg["option_type"],
            "mid_price": mid,
            "expiry_date": exp,
        })
    return out


def unique_leg_expiries(legs: Sequence[Mapping]) -> list[date]:
    """Distinct expiry dates in leg order (first seen)."""
    out: list[date] = []
    seen: set[date] = set()
    for leg in legs:
        exp = expiry_date(leg.get("expiry_date") or leg.get("expiry"))
        if exp is None or exp in seen:
            continue
        seen.add(exp)
        out.append(exp)
    return out


def build_close_suggestion(
    executed_legs: Sequence[Mapping],
    chains_by_expiry: Mapping[Any, Sequence[Mapping]],
    spot: Optional[float],
) -> dict:
    """Per-leg close prices + estimated gross P&L (same formula as live MTM).

    ``chains_by_expiry`` maps expiry (``date`` or ``YYYY-MM-DD``) to FO chain
    rows for that expiry. Same-strike calendar legs therefore get distinct
    mids; same-expiry verticals/condors still resolve by strike + CE/PE.
    """
    out: list[dict] = []
    est = 0.0
    for leg in executed_legs:
        exp = expiry_iso(leg.get("expiry_date") or leg.get("expiry"))
        chain = _chain_for_expiry(chains_by_expiry, leg.get("expiry_date") or exp)
        raw_mid = mid_from_chain_rows(chain, leg["strike"], leg["option_type"])
        mid, src = sanitized_close_price(
            option_type=leg["option_type"],
            strike=float(leg["strike"]),
            raw_mid=raw_mid,
            spot=spot,
        )
        lots = int(leg.get("lots_actual") or leg.get("lots") or 0)
        lot_size = int(leg.get("lot_size") or 0)
        fill = float(leg.get("fill_price") or 0.0)
        action = str(leg.get("action") or "")
        est += leg_close_pnl(
            action=action,
            fill_price=fill,
            close_price=mid,
            lots=lots,
            lot_size=lot_size,
        )
        out.append({
            "leg_order":       leg["leg_order"],
            "action":          action,
            "symbol":          leg.get("symbol"),
            "expiry_date":     exp,
            "strike":          float(leg["strike"]),
            "option_type":     leg["option_type"],
            "fill_price":      fill,
            "lots":            lots,
            "suggested_close": round(mid, 2),
            "price_source":    src,
        })
    return {"legs": out, "est_gross_pnl": round(est, 2)}
