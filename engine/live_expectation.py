"""
engine/live_expectation.py
==========================

Live win-probability and expiry expectation for an OPEN trade.

Recomputes PoP from **current** spot, remaining DTE, and live ATM IV using
the same ``estimate_pop`` model as suggestion-time — fills replace suggested
prices so breakevens match what was actually traded. This is not the frozen
suggestion PoP / EV.

Pure functions. No I/O.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Optional, Sequence

from contracts import SuggestionLeg
from engine.indicators import expected_move
from engine.leg_builder import breakevens, estimate_pop

# Keep in sync with engine.leg_builder._DEBIT_STRATEGIES_PoP.
_DEBIT_STRATEGIES = frozenset({
    "LONG_STRADDLE", "LONG_STRANGLE", "LONG_CALL", "LONG_PUT",
    "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD",
})

_BREAKOUT_STRATEGIES = frozenset({"LONG_STRADDLE", "LONG_STRANGLE"})
_BULL_STRATEGIES = frozenset({
    "BULL_PUT_SPREAD", "JADE_LIZARD", "BULL_CALL_SPREAD", "LONG_CALL",
})
_BEAR_STRATEGIES = frozenset({"BEAR_CALL_SPREAD", "BEAR_PUT_SPREAD", "LONG_PUT"})
_RANGE_STRATEGIES = frozenset({
    "IRON_CONDOR", "IRON_BUTTERFLY", "SHORT_STRANGLE", "CALENDAR_SPREAD",
})

_STANCE_BAND_PP = 5.0
_TIGHT_EM_FRACTION = 0.5
_TREND_BAND_PCT = 0.15


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return n


def normalize_atm_iv(raw: Any) -> Optional[float]:
    """Return IV as a decimal (0.18). Values > 2 are treated as percent."""
    iv = _as_float(raw)
    if iv is None or iv <= 0:
        return None
    if iv > 2.0:
        iv = iv / 100.0
    return iv if iv > 0 else None


def legs_from_fills(
    legs: Sequence[Mapping[str, Any] | Any],
    *,
    underlying: str,
    expiry: date,
) -> list[SuggestionLeg]:
    """Build ``SuggestionLeg`` rows using fill prices as ``suggested_price``."""
    out: list[SuggestionLeg] = []
    for i, raw in enumerate(legs):
        getter = raw.get if isinstance(raw, Mapping) else lambda k, d=None: getattr(raw, k, d)
        fill = _as_float(getter("fill_price") or getter("suggested_price")) or 0.0
        exp = getter("expiry_date") or getter("expiry") or expiry
        key = getter("key")
        if exp is None and isinstance(key, tuple) and len(key) > 1:
            exp = key[1]
        if not isinstance(exp, date):
            exp = expiry
        out.append(SuggestionLeg(
            leg_order=int(getter("leg_order") or (i + 1)),
            hedge_pair_leg=None,
            symbol=str(getter("symbol") or underlying),
            expiry_date=exp,
            strike=float(getter("strike") or 0.0),
            option_type=str(getter("option_type") or "").upper(),
            action=str(getter("action") or "").upper(),
            lots=int(getter("lots") or 1),
            lot_size=int(getter("lot_size") or 0),
            suggested_price=fill,
            suggested_price_low=fill,
            suggested_price_high=fill,
            leg_purpose_note="",
        ))
    return out


def _expiry_pop_from_bes(
    *,
    strategy: str,
    spot: float,
    upper_be: Optional[float],
    lower_be: Optional[float],
) -> float:
    """Deterministic PoP at expiry (DTE = 0): inside/past breakevens or not."""
    debit = strategy in _DEBIT_STRATEGIES
    if debit:
        above = upper_be is not None and spot >= upper_be
        below = lower_be is not None and spot <= lower_be
        return 100.0 if (above or below) else 0.0
    inside = True
    if upper_be is not None and spot > upper_be:
        inside = False
    if lower_be is not None and spot < lower_be:
        inside = False
    if upper_be is None and lower_be is None:
        return 50.0
    return 100.0 if inside else 0.0


def _stance(live_pop: Optional[float], entry_pop: Optional[float]) -> str:
    if live_pop is None or entry_pop is None:
        return "unknown"
    delta = live_pop - entry_pop
    if delta >= _STANCE_BAND_PP:
        return "improving"
    if delta <= -_STANCE_BAND_PP:
        return "weakening"
    return "stable"


def _em_vs_be(
    *,
    strategy: str,
    spot: float,
    em: float,
    upper_be: Optional[float],
    lower_be: Optional[float],
) -> tuple[Optional[str], Optional[float], Optional[float]]:
    """(label, nearest_be, dist_to_be)."""
    candidates = [b for b in (upper_be, lower_be) if b is not None]
    if not candidates:
        return None, None, None
    nearest = min(candidates, key=lambda b: abs(spot - b))
    dist = abs(spot - nearest)
    debit = strategy in _DEBIT_STRATEGIES
    if debit:
        past = (
            (upper_be is not None and spot >= upper_be)
            or (lower_be is not None and spot <= lower_be)
        )
        if not past:
            label = "needs_move"
        elif em > 0 and dist / em < _TIGHT_EM_FRACTION:
            label = "tight"
        else:
            label = "comfortable"
    else:
        outside = (
            (upper_be is not None and spot > upper_be)
            or (lower_be is not None and spot < lower_be)
        )
        if outside:
            label = "outside"
        elif em > 0 and dist / em < _TIGHT_EM_FRACTION:
            label = "tight"
        else:
            label = "comfortable"
    return label, nearest, dist


def _leg_strike(
    legs: Sequence[Mapping[str, Any] | Any],
    action: str,
    option_type: str,
) -> Optional[float]:
    action_u = action.upper()
    ot_u = option_type.upper()
    for raw in legs:
        getter = raw.get if isinstance(raw, Mapping) else lambda k, d=None: getattr(raw, k, d)
        if str(getter("action") or "").upper() != action_u:
            continue
        if str(getter("option_type") or "").upper() != ot_u:
            continue
        strike = _as_float(getter("strike"))
        if strike is not None:
            return strike
    return None


def _spot_trend(spot_change: Optional[float], entry_spot: Optional[float]) -> tuple[str, str]:
    if spot_change is None or entry_spot is None or entry_spot <= 0:
        return "unknown", ""
    pct = spot_change / entry_spot * 100.0
    if pct >= _TREND_BAND_PCT:
        return "up", f"drifting up ({spot_change:+,.0f} vs entry)"
    if pct <= -_TREND_BAND_PCT:
        return "down", f"drifting down ({spot_change:+,.0f} vs entry)"
    return "flat", f"little change ({spot_change:+,.0f} vs entry)"


def assess_direction_fit(
    *,
    strategy: str,
    underlying: str,
    spot: Optional[float],
    upper_be: Optional[float],
    lower_be: Optional[float],
    legs: Sequence[Mapping[str, Any] | Any],
    spot_change: Optional[float] = None,
    entry_spot: Optional[float] = None,
) -> dict:
    """How current spot/move aligns with what this strategy needs to win."""
    strat = (strategy or "").upper()
    name = "Nifty" if str(underlying or "").upper() == "NIFTY" else str(underlying or "Index")
    spot_f = _as_float(spot)
    if spot_f is None:
        return {
            "direction_fit": "unknown",
            "direction_label": "Waiting for spot",
            "direction_detail": "Need spot to judge whether the market is moving the way this trade wants.",
            "spot_trend": "unknown",
        }

    trend, trend_txt = _spot_trend(spot_change, entry_spot)
    sc = _leg_strike(legs, "SELL", "CE")
    sp = _leg_strike(legs, "SELL", "PE")
    bc = _leg_strike(legs, "BUY", "CE")
    bp = _leg_strike(legs, "BUY", "PE")

    in_zone: Optional[bool] = None
    want: str = ""

    if strat in _RANGE_STRATEGIES or strat == "IRON_CONDOR":
        if sp is not None and sc is not None:
            in_zone = sp <= spot_f <= sc
            want = f"{name} stays between ₹{sp:,.0f} and ₹{sc:,.0f}"
        elif lower_be is not None and upper_be is not None:
            in_zone = lower_be <= spot_f <= upper_be
            want = f"{name} stays inside breakevens ₹{lower_be:,.0f}–₹{upper_be:,.0f}"
        else:
            want = f"{name} stays range-bound"
    elif strat in _BREAKOUT_STRATEGIES:
        if upper_be is not None and lower_be is not None:
            in_zone = spot_f >= upper_be or spot_f <= lower_be
            want = f"a sharp move above ₹{upper_be:,.0f} or below ₹{lower_be:,.0f}"
        else:
            want = "a large breakout in either direction"
    elif strat in _BULL_STRATEGIES:
        key = sp or sc or bc
        if key is not None:
            in_zone = spot_f >= key
            want = f"{name} rises or holds above ₹{key:,.0f}"
        elif lower_be is not None:
            in_zone = spot_f >= lower_be
            want = f"{name} stays above ₹{lower_be:,.0f}"
        else:
            want = f"{name} to rally"
    elif strat in _BEAR_STRATEGIES:
        key = sc or sp or bp
        if key is not None:
            in_zone = spot_f <= key
            want = f"{name} falls or holds below ₹{key:,.0f}"
        elif upper_be is not None:
            in_zone = spot_f <= upper_be
            want = f"{name} stays below ₹{upper_be:,.0f}"
        else:
            want = f"{name} to decline"
    else:
        if lower_be is not None and upper_be is not None:
            in_zone = lower_be <= spot_f <= upper_be
            want = f"{name} stays between breakevens"

    if in_zone is True:
        fit = "aligned"
        if strat in _BREAKOUT_STRATEGIES:
            label = "Breakout working"
            detail = f"Spot is outside breakevens — the move this trade paid for. {trend_txt}.".strip()
        elif strat in _BULL_STRATEGIES:
            label = "Bullish setup intact"
            detail = f"Spot is where this trade wants it ({want}). {trend_txt}.".strip()
        elif strat in _BEAR_STRATEGIES:
            label = "Bearish setup intact"
            detail = f"Spot is where this trade wants it ({want}). {trend_txt}.".strip()
        else:
            label = "Range-friendly"
            detail = f"Spot is inside the profit zone — {want.lower()}. {trend_txt}.".strip()
    elif in_zone is False:
        fit = "against"
        if strat in _BREAKOUT_STRATEGIES:
            label = "Still waiting for the move"
            detail = (
                f"Spot is between breakevens; this trade needs {want}. "
                f"Flat days hurt time decay. {trend_txt}."
            ).strip()
        elif strat in _BULL_STRATEGIES:
            label = "Needs a rally"
            detail = f"Spot is below where this trade earns ({want}). {trend_txt}.".strip()
        elif strat in _BEAR_STRATEGIES:
            label = "Needs a decline"
            detail = f"Spot is above where this trade earns ({want}). {trend_txt}.".strip()
        else:
            label = "Outside profit zone"
            detail = f"Spot has moved past breakeven — {want.lower()} is what helps now. {trend_txt}.".strip()
    else:
        fit = "neutral"
        label = "Mixed"
        detail = want or "Spot vs breakeven unclear for this strategy."

    return {
        "direction_fit": fit,
        "direction_label": label,
        "direction_detail": detail.rstrip(" ."),
        "spot_trend": trend,
    }


def resolve_market_inputs(
    db,
    symbol: str,
    expiry: date,
    *,
    live_spot: Optional[float] = None,
    live_iv: Optional[float] = None,
) -> dict:
    """Best available spot/IV: live tick, last intraday sample, or EOD close."""
    from database.models import AtmIvTimeseriesRepo, SpotEodRepo

    sym = str(symbol or "").upper()
    if live_spot is not None and live_spot > 0:
        iv = normalize_atm_iv(live_iv)
        if iv is None:
            iv = normalize_atm_iv(
                AtmIvTimeseriesRepo(db).latest_atm_iv(sym, expiry)
            )
        return {
            "spot": float(live_spot),
            "atm_iv": iv,
            "data_source": "live",
            "data_as_of": None,
        }

    snap = AtmIvTimeseriesRepo(db).latest_snapshot(sym, expiry)
    if snap and snap.get("spot") is not None:
        try:
            spot = float(snap["spot"])
        except (TypeError, ValueError):
            spot = 0.0
        if spot > 0:
            iv = normalize_atm_iv(snap.get("atm_iv"))
            as_of = snap.get("snapshot_at")
            return {
                "spot": spot,
                "atm_iv": iv,
                "data_source": "intraday",
                "data_as_of": as_of.isoformat(timespec="seconds")
                if hasattr(as_of, "isoformat") else str(as_of) if as_of else None,
            }

    eod = SpotEodRepo(db).latest(sym)
    if eod and eod.get("close_price") is not None:
        try:
            spot = float(eod["close_price"])
        except (TypeError, ValueError):
            spot = 0.0
        if spot > 0:
            iv = normalize_atm_iv(
                AtmIvTimeseriesRepo(db).latest_atm_iv(sym, expiry)
            )
            td = eod.get("trade_date")
            return {
                "spot": spot,
                "atm_iv": iv,
                "data_source": "eod",
                "data_as_of": td.isoformat() if hasattr(td, "isoformat") else str(td),
            }

    return {
        "spot": None,
        "atm_iv": normalize_atm_iv(
            AtmIvTimeseriesRepo(db).latest_atm_iv(sym, expiry)
        ),
        "data_source": "none",
        "data_as_of": None,
    }


def compute_trade_outlook(
    db,
    *,
    legs: Sequence[Mapping[str, Any] | Any],
    strategy: str,
    underlying: str,
    expiry: date,
    dte: int,
    max_profit: float,
    max_loss: float,
    entry_pop: Optional[float] = None,
    entry_spot: Optional[float] = None,
    live_spot: Optional[float] = None,
    live_iv: Optional[float] = None,
) -> dict:
    """Outlook for dashboard display — uses live tick or last stored market data."""
    market = resolve_market_inputs(
        db, underlying, expiry, live_spot=live_spot, live_iv=live_iv,
    )
    out = live_trade_outlook(
        legs=legs,
        strategy=strategy,
        underlying=underlying,
        expiry=expiry,
        spot=market.get("spot"),
        dte=dte,
        atm_iv=market.get("atm_iv"),
        max_profit=max_profit,
        max_loss=max_loss,
        entry_pop=entry_pop,
        entry_spot=entry_spot,
        data_source=market.get("data_source"),
        data_as_of=market.get("data_as_of"),
    )
    return out


def _summary(
    *,
    live_pop: Optional[float],
    entry_pop: Optional[float],
    stance: str,
    spot: Optional[float],
    em: Optional[float],
    em_label: Optional[str],
    direction_label: Optional[str] = None,
    data_source: Optional[str] = None,
) -> str:
    if live_pop is None:
        if spot is None:
            return "Waiting for spot to score this trade."
        return "Waiting for ATM IV to score live win chance."
    head = f"Win chance {live_pop:.0f}%"
    if entry_pop is not None:
        head += f" (entry {entry_pop:.0f}%)"
    if stance == "improving":
        head += " — improving"
    elif stance == "weakening":
        head += " — weakening"
    tail: list[str] = []
    if direction_label:
        tail.append(direction_label)
    if spot is not None:
        tail.append(f"Spot {spot:,.0f}")
    if em is not None and em > 0:
        tail.append(f"EM ±{em:,.0f}")
    if em_label == "comfortable":
        tail.append("EM inside BEs")
    elif em_label == "tight":
        tail.append("near a breakeven")
    elif em_label == "outside":
        tail.append("spot past breakeven")
    elif em_label == "needs_move":
        tail.append("still needs a move to BE")
    if data_source == "eod":
        tail.append("EOD close")
    elif data_source == "intraday":
        tail.append("last intraday snapshot")
    return head if not tail else f"{head}. {' · '.join(tail)}"


def live_trade_outlook(
    *,
    legs: Sequence[Mapping[str, Any] | Any],
    strategy: str,
    underlying: str,
    expiry: date,
    spot: Optional[float],
    dte: int,
    atm_iv: Optional[float],
    max_profit: float,
    max_loss: float,
    entry_pop: Optional[float] = None,
    entry_spot: Optional[float] = None,
    data_source: Optional[str] = None,
    data_as_of: Optional[str] = None,
) -> dict:
    """Live outlook dict (JSON-safe) for an open trade.

    ``live_pop`` / ``live_ev`` are None until spot and ATM IV are available
    (except DTE 0, which only needs spot + breakevens).
    """
    spot_f = _as_float(spot)
    entry_spot_f = _as_float(entry_spot)
    entry_pop_f = _as_float(entry_pop)
    iv = normalize_atm_iv(atm_iv)
    dte_i = max(int(dte or 0), 0)
    mp = float(max_profit or 0.0)
    ml = abs(float(max_loss or 0.0))

    sug_legs = legs_from_fills(legs, underlying=underlying, expiry=expiry)
    upper_be = lower_be = None
    if sug_legs:
        try:
            upper_be, lower_be = breakevens(sug_legs, strategy or "")
        except Exception:
            upper_be = lower_be = None

    live_pop: Optional[float] = None
    if spot_f is not None and spot_f > 0 and sug_legs:
        if dte_i <= 0:
            live_pop = _expiry_pop_from_bes(
                strategy=strategy or "",
                spot=spot_f,
                upper_be=upper_be,
                lower_be=lower_be,
            )
        elif iv is not None:
            try:
                live_pop = float(estimate_pop(
                    sug_legs, spot_f, dte_i, iv, chain=None, strategy=strategy or None,
                ))
            except Exception:
                live_pop = None
        if live_pop is not None:
            live_pop = round(max(0.0, min(100.0, live_pop)), 1)

    live_ev: Optional[float] = None
    if live_pop is not None:
        p = live_pop / 100.0
        live_ev = round(p * mp + (1.0 - p) * (-ml), 2)

    em = None
    if spot_f is not None and iv is not None and dte_i > 0:
        em = round(expected_move(spot_f, iv, dte_i), 1)

    em_label, nearest_be, dist_to_be = (None, None, None)
    if spot_f is not None:
        em_label, nearest_be, dist_to_be = _em_vs_be(
            strategy=strategy or "",
            spot=spot_f,
            em=float(em or 0.0),
            upper_be=upper_be,
            lower_be=lower_be,
        )

    pop_delta = None
    if live_pop is not None and entry_pop_f is not None:
        pop_delta = round(live_pop - entry_pop_f, 1)

    spot_change = None
    if spot_f is not None and entry_spot_f is not None:
        spot_change = round(spot_f - entry_spot_f, 2)

    stance = _stance(live_pop, entry_pop_f)
    direction = assess_direction_fit(
        strategy=strategy or "",
        underlying=underlying,
        spot=spot_f,
        upper_be=upper_be,
        lower_be=lower_be,
        legs=legs,
        spot_change=spot_change,
        entry_spot=entry_spot_f,
    )
    summary = _summary(
        live_pop=live_pop,
        entry_pop=entry_pop_f,
        stance=stance,
        spot=spot_f,
        em=em,
        em_label=em_label,
        direction_label=direction.get("direction_label"),
        data_source=data_source,
    )

    return {
        "live_pop": live_pop,
        "live_ev": live_ev,
        "entry_pop": round(entry_pop_f, 1) if entry_pop_f is not None else None,
        "pop_delta": pop_delta,
        "stance": stance,
        "spot": round(spot_f, 2) if spot_f is not None else None,
        "entry_spot": round(entry_spot_f, 2) if entry_spot_f is not None else None,
        "spot_change": spot_change,
        "dte": dte_i,
        "atm_iv": round(iv, 4) if iv is not None else None,
        "expected_move": em,
        "upper_be": round(upper_be, 2) if upper_be is not None else None,
        "lower_be": round(lower_be, 2) if lower_be is not None else None,
        "nearest_be": round(nearest_be, 2) if nearest_be is not None else None,
        "dist_to_be": round(dist_to_be, 2) if dist_to_be is not None else None,
        "em_vs_be": em_label,
        "summary": summary,
        "direction_fit": direction.get("direction_fit"),
        "direction_label": direction.get("direction_label"),
        "direction_detail": direction.get("direction_detail"),
        "spot_trend": direction.get("spot_trend"),
        "data_source": data_source,
        "data_as_of": data_as_of,
    }
