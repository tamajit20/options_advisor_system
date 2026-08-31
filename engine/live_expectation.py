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

import json
from dataclasses import replace
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from contracts import SuggestionLeg
from engine.em_calibration import band_dte, compute_calibration_warning
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


def outlook_horizon(
    *,
    strategy: str,
    legs: Sequence[Mapping[str, Any] | Any],
    fallback_expiry: date,
    as_of: date,
) -> dict:
    """DTE / expiry for live outlook — calendars use the *near* leg horizon."""
    from engine.exit_pricing import expiry_date, unique_leg_expiries
    from utils import days_between

    expiries: list[date] = []
    for raw in legs or []:
        exp = None
        if isinstance(raw, Mapping):
            exp = expiry_date(raw.get("expiry_date") or raw.get("expiry"))
        else:
            key = getattr(raw, "key", None)
            if isinstance(key, tuple) and len(key) > 1:
                exp = expiry_date(key[1])
            if exp is None:
                exp = expiry_date(getattr(raw, "expiry_date", None))
        if exp is not None and exp not in expiries:
            expiries.append(exp)
    if not expiries:
        expiries = unique_leg_expiries(legs) if legs else []
    if not expiries:
        expiries = [fallback_expiry]

    near = min(expiries)
    far = max(expiries)
    near_dte = max(days_between(as_of, near), 0)
    far_dte = max(days_between(as_of, far), 0)
    multi = len(expiries) >= 2 and near != far
    # Only CALENDAR_SPREAD is multi-expiry today; near leg drives PoP / EV.
    # Single-expiry strategies: min(leg expiries) equals the shared expiry.
    outlook_expiry, outlook_dte = near, near_dte

    out = {
        "outlook_expiry": outlook_expiry,
        "outlook_dte": outlook_dte,
        "near_expiry": near.isoformat(),
        "near_dte": near_dte,
        "dte": outlook_dte,
    }
    if multi:
        out["far_expiry"] = far.isoformat()
        out["far_dte"] = far_dte
    return out


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
        from engine.exit_pricing import expiry_date as _parse_expiry
        parsed = _parse_expiry(exp)
        if parsed is not None:
            exp = parsed
        elif not isinstance(exp, date):
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
    strat = (strategy or "").upper()
    if strat in _DEBIT_STRATEGIES:
        above = upper_be is not None and spot >= upper_be
        below = lower_be is not None and spot <= lower_be
        return 100.0 if (above or below) else 0.0
    # Calendar + credit range structures: profit inside breakevens at expiry.
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


def _inside_between(spot: float, lo: Optional[float], hi: Optional[float]) -> Optional[bool]:
    if lo is not None and hi is not None:
        return lo <= spot <= hi
    return None


def _near_strike(spot: float, strike: float, band_pct: float = 0.005) -> bool:
    if strike <= 0:
        return False
    return abs(spot - strike) / strike <= band_pct


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
    """How current spot aligns with this strategy's breakeven / structural need.

    This is **not** MTM P&L — spot can be past a breakeven while marks are still green.
    """
    strat = (strategy or "").upper()
    name = "Nifty" if str(underlying or "").upper() == "NIFTY" else str(underlying or "Index")
    spot_f = _as_float(spot)
    if spot_f is None:
        return {
            "direction_fit": "unknown",
            "direction_label": "Waiting for spot",
            "direction_detail": "Need spot to judge structural fit vs this strategy.",
            "spot_trend": "unknown",
        }

    trend, trend_txt = _spot_trend(spot_change, entry_spot)
    sc = _leg_strike(legs, "SELL", "CE")
    sp = _leg_strike(legs, "SELL", "PE")
    bc = _leg_strike(legs, "BUY", "CE")
    bp = _leg_strike(legs, "BUY", "PE")
    ub = _as_float(upper_be)
    lb = _as_float(lower_be)

    in_zone: Optional[bool] = None
    want: str = ""

    if strat == "IRON_CONDOR":
        inside = _inside_between(spot_f, lb, ub)
        if inside is not None:
            in_zone = inside
            want = f"{name} stays inside breakevens ₹{lb:,.0f}–₹{ub:,.0f}"
        elif sp is not None and sc is not None:
            in_zone = sp <= spot_f <= sc
            want = f"{name} stays between short strikes ₹{sp:,.0f}–₹{sc:,.0f}"
    elif strat == "IRON_BUTTERFLY":
        atm = sc or sp
        inside = _inside_between(spot_f, lb, ub)
        if inside is not None:
            in_zone = inside
            pin = f"₹{atm:,.0f}" if atm else "ATM"
            want = f"{name} pins near {pin} (inside breakevens)"
        elif atm is not None:
            in_zone = _near_strike(spot_f, atm)
            want = f"{name} stays near ₹{atm:,.0f}"
    elif strat == "CALENDAR_SPREAD":
        atm = sc or sp
        inside = _inside_between(spot_f, lb, ub)
        if inside is not None:
            in_zone = inside
            want = f"{name} stays calm inside breakevens through near expiry"
        elif atm is not None:
            in_zone = _near_strike(spot_f, atm, band_pct=0.01)
            want = f"{name} stays near ₹{atm:,.0f} without a large move"
    elif strat == "SHORT_STRANGLE":
        inside = _inside_between(spot_f, lb, ub)
        if inside is not None:
            in_zone = inside
            want = f"{name} stays inside breakevens ₹{lb:,.0f}–₹{ub:,.0f}"
        elif sp is not None and sc is not None:
            in_zone = sp <= spot_f <= sc
            want = f"{name} stays between short strikes ₹{sp:,.0f}–₹{sc:,.0f}"
    elif strat in _BREAKOUT_STRATEGIES:
        if ub is not None and lb is not None:
            in_zone = spot_f >= ub or spot_f <= lb
            want = f"a sharp move above ₹{ub:,.0f} or below ₹{lb:,.0f}"
        else:
            want = "a large breakout in either direction"
    elif strat == "LONG_CALL":
        if ub is not None:
            in_zone = spot_f >= ub
            want = f"{name} above breakeven ₹{ub:,.0f}"
        elif bc is not None:
            in_zone = spot_f >= bc
            want = f"{name} above call strike ₹{bc:,.0f}"
        else:
            want = f"{name} to rally"
    elif strat == "LONG_PUT":
        if lb is not None:
            in_zone = spot_f <= lb
            want = f"{name} below breakeven ₹{lb:,.0f}"
        elif bp is not None:
            in_zone = spot_f <= bp
            want = f"{name} below put strike ₹{bp:,.0f}"
        else:
            want = f"{name} to decline"
    elif strat == "BULL_PUT_SPREAD":
        key = lb if lb is not None else sp
        if key is not None:
            in_zone = spot_f >= key
            want = f"{name} at or above ₹{key:,.0f}"
        else:
            want = f"{name} to hold above the short put"
    elif strat == "BEAR_CALL_SPREAD":
        key = ub if ub is not None else sc
        if key is not None:
            in_zone = spot_f <= key
            want = f"{name} at or below ₹{key:,.0f}"
        else:
            want = f"{name} to stay below the short call"
    elif strat == "JADE_LIZARD":
        key = lb if lb is not None else sp
        if key is not None:
            in_zone = spot_f >= key
            want = f"{name} flat or above ₹{key:,.0f} (no upside loss)"
        else:
            want = f"{name} flat or rallying"
    elif strat == "BULL_CALL_SPREAD":
        key = lb if lb is not None else sc
        if key is not None:
            in_zone = spot_f >= key
            want = f"{name} at or above ₹{key:,.0f}"
        else:
            want = f"{name} to rally"
    elif strat == "BEAR_PUT_SPREAD":
        key = ub if ub is not None else sp
        if key is not None:
            in_zone = spot_f <= key
            want = f"{name} at or below ₹{key:,.0f}"
        else:
            want = f"{name} to fall"
    else:
        inside = _inside_between(spot_f, lb, ub)
        if inside is not None:
            in_zone = inside
            want = f"{name} stays between breakevens"

    if in_zone is True:
        fit = "aligned"
        if strat in _BREAKOUT_STRATEGIES:
            label = "Breakout working"
            detail = f"Spot is outside breakevens — the move this trade paid for. {trend_txt}.".strip()
        elif strat in _BULL_STRATEGIES:
            label = "Bullish structure intact"
            detail = f"Spot meets what this trade needs ({want}). {trend_txt}.".strip()
        elif strat in _BEAR_STRATEGIES:
            label = "Bearish structure intact"
            detail = f"Spot meets what this trade needs ({want}). {trend_txt}.".strip()
        elif strat == "IRON_BUTTERFLY":
            label = "Pin-friendly"
            detail = f"Spot is near the body — {want.lower()}. {trend_txt}.".strip()
        elif strat == "CALENDAR_SPREAD":
            label = "Quiet market"
            detail = f"Spot is calm enough — {want.lower()}. {trend_txt}.".strip()
        else:
            label = "Inside breakevens"
            detail = f"Spot is inside breakevens — {want.lower()}. {trend_txt}.".strip()
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
            detail = f"Spot is below the structural level ({want}). MTM can still be green from theta/IV. {trend_txt}.".strip()
        elif strat in _BEAR_STRATEGIES:
            label = "Needs a decline"
            detail = f"Spot is above the structural level ({want}). MTM can still be green from theta/IV. {trend_txt}.".strip()
        elif strat == "IRON_BUTTERFLY":
            label = "Away from the pin"
            detail = f"Spot has drifted from the body — {want.lower()} helps. {trend_txt}.".strip()
        elif strat == "CALENDAR_SPREAD":
            label = "Too much movement"
            detail = f"Spot moved away from the calendar body — {want.lower()}. {trend_txt}.".strip()
        else:
            label = "Past breakeven"
            detail = (
                f"Spot is outside breakevens ({want.lower()}). "
                f"This is structural — you can still show MTM profit. {trend_txt}."
            ).strip()
    else:
        fit = "neutral"
        label = "Unclear"
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
    current_mtm: Optional[float] = None,
    leg_ltps: Optional[Mapping[str, Any]] = None,
    conditions_json: Any = None,
    include_scenarios: bool = True,
    as_of: Optional[date] = None,
) -> dict:
    """Outlook for dashboard display — uses live tick or last stored market data."""
    from utils import now_ist

    as_of = as_of or now_ist().date()
    horizon = outlook_horizon(
        strategy=strategy, legs=legs, fallback_expiry=expiry, as_of=as_of,
    )
    outlook_expiry = horizon["outlook_expiry"]
    outlook_dte = int(horizon["outlook_dte"])
    market = resolve_market_inputs(
        db, underlying, outlook_expiry, live_spot=live_spot, live_iv=live_iv,
    )
    base = live_trade_outlook(
        legs=legs,
        strategy=strategy,
        underlying=underlying,
        expiry=outlook_expiry,
        spot=market.get("spot"),
        dte=outlook_dte,
        atm_iv=market.get("atm_iv"),
        max_profit=max_profit,
        max_loss=max_loss,
        entry_pop=entry_pop,
        entry_spot=entry_spot,
        data_source=market.get("data_source"),
        data_as_of=market.get("data_as_of"),
        leg_ltps=leg_ltps,
    )
    em_warn = _em_calibration_for_trade(db, underlying, outlook_dte)
    enriched = enrich_trade_outlook(
        base,
        current_mtm=current_mtm,
        conditions_json=conditions_json,
        em_calibration_warning=em_warn,
        include_scenarios=include_scenarios,
        legs=legs,
        strategy=strategy,
        underlying=underlying,
        expiry=outlook_expiry,
        dte=outlook_dte,
        atm_iv=market.get("atm_iv"),
        max_profit=max_profit,
        max_loss=max_loss,
        leg_ltps=leg_ltps,
    )
    enriched.update({k: v for k, v in horizon.items() if k not in enriched})
    return enriched


def _em_calibration_for_trade(db, underlying: str, dte: int) -> Optional[str]:
    try:
        from config import STRATEGY_CONFIG
        from database.models import EmCalibrationRepo

        min_n = int(STRATEGY_CONFIG.get("em_calibration_min_samples") or 4)
        thresh = float(STRATEGY_CONFIG.get("em_calibration_deviation_threshold") or 0.25)
        limit = int(STRATEGY_CONFIG.get("em_calibration_lookback_limit") or 12)
        band = band_dte(dte)
        samples = EmCalibrationRepo(db).recent_ratios(underlying, band, limit)
        return compute_calibration_warning(
            samples, underlying=underlying, dte=dte,
            min_samples=min_n, deviation_threshold=thresh,
        )
    except Exception:
        return None


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


def compute_expiry_ev(live_pop: Optional[float], max_profit: float, max_loss: float) -> Optional[float]:
    if live_pop is None:
        return None
    p = float(live_pop) / 100.0
    mp = float(max_profit or 0.0)
    ml = abs(float(max_loss or 0.0))
    return round(p * mp + (1.0 - p) * (-ml), 2)


def _apply_live_leg_prices(
    sug_legs: list[SuggestionLeg],
    leg_ltps: Optional[Mapping[str, Any]],
) -> list[SuggestionLeg]:
    if not leg_ltps or not sug_legs:
        return sug_legs
    from engine.exit_pricing import _legacy_leg_quote_key, format_leg_quote_key

    out: list[SuggestionLeg] = []
    for sl in sug_legs:
        keys = [
            format_leg_quote_key(sl.symbol, sl.expiry_date, sl.strike, sl.option_type),
            _legacy_leg_quote_key(sl.symbol, sl.strike, sl.option_type),
        ]
        live_p = None
        for k in keys:
            if k in leg_ltps:
                live_p = _as_float(leg_ltps[k])
                break
        if live_p is not None and live_p > 0:
            out.append(replace(
                sl,
                suggested_price=live_p,
                suggested_price_low=live_p,
                suggested_price_high=live_p,
            ))
        else:
            out.append(sl)
    return out


def _be_distance_detail(
    spot: Optional[float],
    upper_be: Optional[float],
    lower_be: Optional[float],
) -> dict:
    spot_f = _as_float(spot)
    ub = _as_float(upper_be)
    lb = _as_float(lower_be)
    if spot_f is None:
        return {}
    if lb is not None and spot_f < lb:
        pts = round(lb - spot_f, 2)
        pct = round(pts / spot_f * 100.0, 2) if spot_f > 0 else None
        text = f"₹{pts:,.0f} below lower BE ({pct:.2f}%)" if pct is not None else f"₹{pts:,.0f} below lower BE"
        return {"be_side": "below_lower", "be_distance_pts": pts, "be_distance_pct": pct, "be_distance_text": text}
    if ub is not None and spot_f > ub:
        pts = round(spot_f - ub, 2)
        pct = round(pts / spot_f * 100.0, 2) if spot_f > 0 else None
        text = f"₹{pts:,.0f} above upper BE ({pct:.2f}%)" if pct is not None else f"₹{pts:,.0f} above upper BE"
        return {"be_side": "above_upper", "be_distance_pts": pts, "be_distance_pct": pct, "be_distance_text": text}
    if lb is not None and ub is not None:
        mid = (lb + ub) / 2.0
        pts = round(abs(spot_f - mid), 2)
        return {
            "be_side": "inside",
            "be_distance_pts": pts,
            "be_distance_pct": round(pts / spot_f * 100.0, 2) if spot_f > 0 else None,
            "be_distance_text": f"Inside breakevens (₹{pts:,.0f} from mid)",
        }
    return {"be_side": "unknown"}


def parse_entry_regime(conditions_json: Any) -> dict:
    raw = conditions_json
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, list):
        return {}
    import re
    out: dict = {}
    for c in raw:
        if not isinstance(c, dict):
            continue
        detail = str(c.get("detail") or "")
        label = str(c.get("label") or "").lower()
        if "iv rank" in label:
            m = re.search(r"IV Rank\s+([\d.]+)", detail, re.I)
            if m:
                out["iv_rank"] = float(m.group(1))
        if "vix" in label:
            m = re.search(r"VIX regime:\s*(\w+)", detail, re.I)
            if m:
                out["vix_regime"] = m.group(1).upper()
        if "trend" in label:
            m = re.search(r"Trend:\s*(\w+)", detail, re.I)
            if m:
                out["trend"] = m.group(1).upper()
        if "pcr" in label:
            m = re.search(r"PCR\s+([\d.]+)", detail, re.I)
            if m:
                out["pcr"] = float(m.group(1))
    return out


def regime_context_note(*, entry_regime: dict, spot_trend: str, strategy: str) -> Optional[str]:
    entry_trend = (entry_regime.get("trend") or "").upper()
    if not entry_trend:
        return None
    strat = (strategy or "").upper()
    parts = [f"Entered {entry_trend.lower()}"]
    if entry_regime.get("iv_rank") is not None:
        parts.append(f"IV rank {entry_regime['iv_rank']:.0f}")
    if spot_trend in ("up", "down", "flat"):
        parts.append(f"spot now {spot_trend}")
    note = " · ".join(parts)
    if entry_trend == "SIDEWAYS" and spot_trend == "down" and strat in _RANGE_STRATEGIES:
        return note + " — drift hurts range/credit trades"
    if entry_trend == "SIDEWAYS" and spot_trend == "up" and strat in _RANGE_STRATEGIES:
        return note + " — upside drift tests short call side"
    if entry_trend == "BULLISH" and spot_trend == "down" and strat in _BULL_STRATEGIES:
        return note + " — weak vs entry thesis"
    if entry_trend == "BEARISH" and spot_trend == "up" and strat in _BEAR_STRATEGIES:
        return note + " — weak vs entry thesis"
    return note


def hold_vs_close_advice(
    *,
    current_mtm: Optional[float],
    hold_ev: Optional[float],
    direction_fit: Optional[str],
    strategy: str = "",
) -> Optional[str]:
    mtm = _as_float(current_mtm)
    hev = _as_float(hold_ev)
    if mtm is None or hev is None:
        return None
    if mtm > 0 and hev < 0:
        return (
            f"Close now (MTM ₹{mtm:,.0f}) — modeled near-expiry EV is ₹{hev:,.0f}. "
            f"Expiry EV is total P&L if held, not extra gain from today."
        )
    gap = mtm - hev
    if gap > 500:
        if direction_fit == "against":
            return f"Closing now (MTM ₹{mtm:,.0f}) beats hold EV (₹{hev:,.0f}) — consider booking."
        return f"Closing now (₹{mtm:,.0f}) exceeds hold EV (₹{hev:,.0f}) — optional profit lock."
    if hev - mtm > 500:
        return f"Hold EV (₹{hev:,.0f}) favours staying vs MTM ₹{mtm:,.0f}."
    return None


def expiry_ev_note(
    *,
    strategy: str,
    max_profit: float,
    max_loss: float,
    near_dte: Optional[int],
    current_mtm: Optional[float],
) -> Optional[str]:
    """Clarify that Expiry EV is modeled total P&L, not incremental gain from MTM."""
    strat = (strategy or "").upper()
    mtm = _as_float(current_mtm)
    mp = float(max_profit or 0.0)
    if strat != "CALENDAR_SPREAD" or mp <= 0:
        return None
    nd = int(near_dte) if near_dte is not None else None
    bits = [
        f"Blends win ({mp:,.0f} max) vs loss (−{abs(float(max_loss or 0.0)):,.0f}) — "
        "total P&L at near expiry, not profit on top of today's MTM.",
    ]
    if mtm is not None:
        bits.append(f"Close now = MTM ₹{mtm:,.0f}.")
    if nd is not None and nd <= 3:
        bits.append(f"Near leg {nd} DTE.")
    return " ".join(bits)


def _data_source_label(source: Optional[str], as_of: Optional[str]) -> str:
    if source == "live":
        return "Live"
    if source == "intraday":
        return f"Intraday {as_of}" if as_of else "Last intraday"
    if source == "eod":
        return f"EOD {as_of}" if as_of else "EOD close"
    return ""


def compute_scenarios(
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
    leg_ltps: Optional[Mapping[str, Any]] = None,
) -> list[dict]:
    spot_f = _as_float(spot)
    if spot_f is None or spot_f <= 0:
        return []
    out: list[dict] = []
    for label, pct in (("Flat", 0.0), ("Spot −1%", -0.01), ("Spot +1%", 0.01)):
        scen_spot = round(spot_f * (1.0 + pct), 2)
        row = live_trade_outlook(
            legs=legs, strategy=strategy, underlying=underlying, expiry=expiry,
            spot=scen_spot, dte=dte, atm_iv=atm_iv, max_profit=max_profit, max_loss=max_loss,
            leg_ltps=leg_ltps,
        )
        out.append({
            "label": label, "spot": scen_spot,
            "live_pop": row.get("live_pop"), "live_ev": row.get("live_ev"),
            "direction_label": row.get("direction_label"),
        })
    return out


def enrich_trade_outlook(
    base: dict,
    *,
    current_mtm: Optional[float] = None,
    conditions_json: Any = None,
    em_calibration_warning: Optional[str] = None,
    include_scenarios: bool = True,
    legs: Optional[Sequence[Mapping[str, Any] | Any]] = None,
    strategy: str = "",
    underlying: str = "",
    expiry: Optional[date] = None,
    dte: int = 0,
    atm_iv: Optional[float] = None,
    max_profit: float = 0.0,
    max_loss: float = 0.0,
    leg_ltps: Optional[Mapping[str, Any]] = None,
) -> dict:
    out = dict(base)
    out["entry_ev"] = compute_expiry_ev(out.get("entry_pop"), max_profit, max_loss)
    out["close_now_ev"] = round(float(current_mtm), 2) if current_mtm is not None else None
    out.update(_be_distance_detail(out.get("spot"), out.get("upper_be"), out.get("lower_be")))
    out["data_source_label"] = _data_source_label(out.get("data_source"), out.get("data_as_of"))
    entry_regime = parse_entry_regime(conditions_json)
    out["entry_regime"] = entry_regime
    regime = regime_context_note(
        entry_regime=entry_regime, spot_trend=str(out.get("spot_trend") or "unknown"), strategy=strategy,
    )
    if regime:
        out["regime_note"] = regime
    if em_calibration_warning:
        out["em_calibration_warning"] = em_calibration_warning
    out["hold_vs_close"] = hold_vs_close_advice(
        current_mtm=current_mtm,
        hold_ev=out.get("live_ev"),
        direction_fit=out.get("direction_fit"),
        strategy=strategy,
    )
    mtm_f = _as_float(current_mtm)
    if mtm_f is not None and out.get("live_ev") is not None:
        out["ev_from_now"] = round(float(out["live_ev"]) - mtm_f, 2)
    out["ev_note"] = expiry_ev_note(
        strategy=strategy,
        max_profit=max_profit,
        max_loss=max_loss,
        near_dte=out.get("near_dte") if out.get("near_dte") is not None else dte,
        current_mtm=current_mtm,
    )
    if include_scenarios and legs is not None and expiry is not None:
        out["scenarios"] = compute_scenarios(
            legs=legs, strategy=strategy, underlying=underlying, expiry=expiry,
            spot=out.get("spot"), dte=dte, atm_iv=atm_iv or out.get("atm_iv"),
            max_profit=max_profit, max_loss=max_loss, leg_ltps=leg_ltps,
        )
    return out


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
    leg_ltps: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Live outlook dict (JSON-safe) for an open trade.

    ``live_pop`` / ``live_ev`` are None until spot and ATM IV are available
    (except DTE 0, which only needs spot + breakevens).
    """
    from utils import now_ist

    as_of = now_ist().date()
    horizon = outlook_horizon(
        strategy=strategy or "",
        legs=legs,
        fallback_expiry=expiry,
        as_of=as_of,
    )
    outlook_expiry = horizon["outlook_expiry"]
    spot_f = _as_float(spot)
    entry_spot_f = _as_float(entry_spot)
    entry_pop_f = _as_float(entry_pop)
    iv = normalize_atm_iv(atm_iv)
    dte_i = max(int(horizon["outlook_dte"]), 0)
    mp = float(max_profit or 0.0)
    ml = abs(float(max_loss or 0.0))

    sug_legs_fill = legs_from_fills(legs, underlying=underlying, expiry=outlook_expiry)
    # PoP / breakevens use entry fills — live marks on calendars can collapse the
    # BE envelope (near decay vs far) and falsely inflate win chance via delta PoP.
    pop_legs = sug_legs_fill
    uses_live_marks = bool(leg_ltps and sug_legs_fill)
    upper_be = lower_be = None
    if pop_legs:
        try:
            upper_be, lower_be = breakevens(pop_legs, strategy or "")
        except Exception:
            upper_be = lower_be = None

    live_pop: Optional[float] = None
    if spot_f is not None and spot_f > 0 and pop_legs:
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
                    pop_legs, spot_f, dte_i, iv, chain=None, strategy=strategy or None,
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

    result = {
        "live_pop": live_pop,
        "live_ev": live_ev,
        "entry_pop": round(entry_pop_f, 1) if entry_pop_f is not None else None,
        "pop_delta": pop_delta,
        "stance": stance,
        "spot": round(spot_f, 2) if spot_f is not None else None,
        "entry_spot": round(entry_spot_f, 2) if entry_spot_f is not None else None,
        "spot_change": spot_change,
        "dte": dte_i,
        "near_dte": horizon.get("near_dte"),
        "far_dte": horizon.get("far_dte"),
        "near_expiry": horizon.get("near_expiry"),
        "far_expiry": horizon.get("far_expiry"),
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
        "uses_live_marks": uses_live_marks,
    }
    if horizon.get("far_dte") is not None and (strategy or "").upper() == "CALENDAR_SPREAD":
        result["ev_horizon_note"] = (
            f"Win chance / EV use near leg ({horizon['near_dte']} DTE); "
            f"far leg {horizon['far_dte']} DTE still open after that."
        )
    return result
