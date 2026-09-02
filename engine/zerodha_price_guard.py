"""
engine/zerodha_price_guard.py
=============================

Live-price gate before Zerodha order placement. Ensures the user cannot
execute a suggestion whose legs have drifted away from the suggested bands
(or mid-price tolerance when bands are missing).

Pure logic — no DB / no Kite calls. Callers supply live LTP per leg_order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from config import ZERODHA_EXECUTION_CONFIG


@dataclass(frozen=True)
class PriceGuardResult:
    ok: bool
    vetoes: List[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def reason(self) -> str:
        return "; ".join(self.vetoes) if self.vetoes else "OK"


def validate_live_prices(
    legs: Iterable[dict],
    live_ltp_by_leg: Dict[int, float],
    *,
    require_band: Optional[bool] = None,
    max_drift_pct: Optional[float] = None,
) -> PriceGuardResult:
    """Check every leg has a live quote and is within allowed price bounds."""
    require_band = (
        ZERODHA_EXECUTION_CONFIG["require_price_band"]
        if require_band is None
        else require_band
    )
    max_drift_pct = (
        float(ZERODHA_EXECUTION_CONFIG["max_price_drift_pct"])
        if max_drift_pct is None
        else float(max_drift_pct)
    )

    vetoes: List[str] = []
    details: dict = {"legs": {}}

    for leg in legs:
        lo = int(leg["leg_order"])
        ltp = live_ltp_by_leg.get(lo)
        if ltp is None:
            vetoes.append(f"leg {lo}: live price unavailable")
            continue

        suggested = leg.get("suggested_price")
        band_lo = leg.get("suggested_price_low")
        band_hi = leg.get("suggested_price_high")
        leg_detail = {
            "ltp": ltp,
            "suggested": suggested,
            "band_lo": band_lo,
            "band_hi": band_hi,
        }

        if band_lo is not None and band_hi is not None and require_band:
            blo = float(band_lo)
            bhi = float(band_hi)
            if blo > 0 and ltp < blo:
                vetoes.append(
                    f"leg {lo}: LTP ₹{ltp:.2f} below band ₹{blo:.2f}"
                )
            if bhi > 0 and ltp > bhi:
                vetoes.append(
                    f"leg {lo}: LTP ₹{ltp:.2f} above band ₹{bhi:.2f}"
                )
            leg_detail["check"] = "band"
        elif suggested is not None and max_drift_pct > 0:
            mid = float(suggested)
            if mid <= 0:
                vetoes.append(f"leg {lo}: invalid suggested_price")
            else:
                drift_pct = abs(ltp - mid) / mid * 100.0
                leg_detail["drift_pct"] = round(drift_pct, 2)
                leg_detail["check"] = "drift"
                if drift_pct > max_drift_pct:
                    vetoes.append(
                        f"leg {lo}: LTP ₹{ltp:.2f} is {drift_pct:.1f}% from "
                        f"suggested ₹{mid:.2f} (max {max_drift_pct:.1f}%)"
                    )
        else:
            leg_detail["check"] = "skipped"

        details["legs"][lo] = leg_detail

    return PriceGuardResult(ok=not vetoes, vetoes=vetoes, details=details)


def validate_limit_prices(
    legs: Iterable[dict],
    limit_by_leg: Dict[int, float],
    *,
    require_band: Optional[bool] = None,
    max_drift_pct: Optional[float] = None,
) -> PriceGuardResult:
    """Check each leg's LIMIT order price against suggestion band / drift."""
    require_band = (
        ZERODHA_EXECUTION_CONFIG["require_price_band"]
        if require_band is None
        else require_band
    )
    max_drift_pct = (
        float(ZERODHA_EXECUTION_CONFIG["max_price_drift_pct"])
        if max_drift_pct is None
        else float(max_drift_pct)
    )

    vetoes: List[str] = []
    details: dict = {"legs": {}}

    for leg in legs:
        lo = int(leg["leg_order"])
        limit_px = limit_by_leg.get(lo)
        if limit_px is None:
            vetoes.append(f"leg {lo}: limit price missing")
            continue

        suggested = leg.get("suggested_price")
        band_lo = leg.get("suggested_price_low")
        band_hi = leg.get("suggested_price_high")
        leg_detail = {
            "limit_price": limit_px,
            "suggested": suggested,
            "band_lo": band_lo,
            "band_hi": band_hi,
        }

        if band_lo is not None and band_hi is not None and require_band:
            blo = float(band_lo)
            bhi = float(band_hi)
            if blo > 0 and limit_px < blo:
                vetoes.append(
                    f"leg {lo}: limit ₹{limit_px:.2f} below band ₹{blo:.2f}"
                )
            if bhi > 0 and limit_px > bhi:
                vetoes.append(
                    f"leg {lo}: limit ₹{limit_px:.2f} above band ₹{bhi:.2f}"
                )
            leg_detail["check"] = "band"
        elif suggested is not None and max_drift_pct > 0:
            mid = float(suggested)
            if mid <= 0:
                vetoes.append(f"leg {lo}: invalid suggested_price")
            else:
                drift_pct = abs(limit_px - mid) / mid * 100.0
                leg_detail["drift_pct"] = round(drift_pct, 2)
                leg_detail["check"] = "drift"
                if drift_pct > max_drift_pct:
                    vetoes.append(
                        f"leg {lo}: limit ₹{limit_px:.2f} is {drift_pct:.1f}% from "
                        f"suggested ₹{mid:.2f} (max {max_drift_pct:.1f}%)"
                    )
        else:
            leg_detail["check"] = "skipped"

        details["legs"][lo] = leg_detail

    return PriceGuardResult(ok=not vetoes, vetoes=vetoes, details=details)


def leg_limit_in_band(leg: dict, limit_price: float) -> bool:
    """True when ``limit_price`` passes the same band/drift rules."""
    lo = int(leg["leg_order"])
    return validate_limit_prices([leg], {lo: limit_price}).ok
