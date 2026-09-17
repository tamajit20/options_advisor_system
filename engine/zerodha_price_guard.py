"""
engine/zerodha_price_guard.py
=============================

Live-price gate before Zerodha order placement. Ensures the user cannot
execute a suggestion whose legs have drifted away from the suggested bands
(or mid-price tolerance when bands are missing).

Multi-leg structures are gated on combined (net) credit/debit when every leg
has a band: individual legs may sit outside their own band if the structure
net is still at or better than the combined acceptable floor (same math as
the dashboard live-premium verdict).

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


_HARD_VETO_MARKERS = (
    "unavailable",
    "missing",
    "invalid suggested",
)


def structure_net_acceptable(
    legs: Iterable[dict],
    price_by_leg: Dict[int, float],
) -> Optional[dict]:
    """Evaluate combined credit/debit vs the structure band envelope.

    Returns ``None`` when the set cannot be evaluated (fewer than two legs,
    missing prices, or missing bands). Otherwise a dict with ``net``,
    ``net_lo``, ``net_hi``, and ``ok`` (True when net is within range or
    better than the suggested high — i.e. ``net >= net_lo``).
    """
    leg_list = list(legs)
    if len(leg_list) < 2:
        return None

    net = 0.0
    net_lo_acc = 0.0
    net_hi_acc = 0.0
    for leg in leg_list:
        lo = int(leg["leg_order"])
        px = price_by_leg.get(lo)
        if px is None:
            return None
        band_lo = leg.get("suggested_price_low")
        band_hi = leg.get("suggested_price_high")
        if band_lo is None or band_hi is None:
            return None
        blo = float(band_lo)
        bhi = float(band_hi)
        if blo <= 0 or bhi <= 0:
            return None
        action = str(leg.get("action") or "").upper()
        sign = 1 if action == "SELL" else -1
        net += sign * float(px)
        # Worst/best net credit: SELL contributes low/high; BUY contributes
        # −high/−low (same envelope as dashboard creditBreakdownHtml).
        net_lo_acc += sign * (blo if action == "SELL" else bhi)
        net_hi_acc += sign * (bhi if action == "SELL" else blo)

    net_lo = min(net_lo_acc, net_hi_acc)
    net_hi = max(net_lo_acc, net_hi_acc)
    return {
        "net": round(net, 4),
        "net_lo": round(net_lo, 4),
        "net_hi": round(net_hi, 4),
        "ok": net >= net_lo,
    }


def _has_hard_vetoes(vetoes: List[str]) -> bool:
    return any(
        any(marker in v.lower() for marker in _HARD_VETO_MARKERS)
        for v in vetoes
    )


def _apply_net_structure_override(
    result: PriceGuardResult,
    legs: Iterable[dict],
    price_by_leg: Dict[int, float],
) -> PriceGuardResult:
    """Pass band/drift vetoes when structure net is still acceptable."""
    if result.ok or _has_hard_vetoes(result.vetoes):
        return result

    net_info = structure_net_acceptable(legs, price_by_leg)
    if net_info is None:
        return result

    details = dict(result.details)
    details["net_structure"] = net_info
    if not net_info["ok"]:
        vetoes = list(result.vetoes) + [
            f"net ₹{net_info['net']:.2f} below minimum "
            f"₹{net_info['net_lo']:.2f}"
        ]
        return PriceGuardResult(ok=False, vetoes=vetoes, details=details)

    details["net_override"] = True
    details["per_leg_vetoes"] = list(result.vetoes)
    return PriceGuardResult(ok=True, vetoes=[], details=details)


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

    leg_list = list(legs)
    vetoes: List[str] = []
    details: dict = {"legs": {}}

    for leg in leg_list:
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

    result = PriceGuardResult(ok=not vetoes, vetoes=vetoes, details=details)
    return _apply_net_structure_override(result, leg_list, live_ltp_by_leg)


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

    leg_list = list(legs)
    vetoes: List[str] = []
    details: dict = {"legs": {}}

    for leg in leg_list:
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

    result = PriceGuardResult(ok=not vetoes, vetoes=vetoes, details=details)
    return _apply_net_structure_override(result, leg_list, limit_by_leg)


def leg_limit_in_band(leg: dict, limit_price: float) -> bool:
    """True when ``limit_price`` passes the same band/drift rules."""
    lo = int(leg["leg_order"])
    return validate_limit_prices([leg], {lo: limit_price}).ok
