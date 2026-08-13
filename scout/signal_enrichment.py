"""Enrich scout signals with entry band, validity, conditions, and live status."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from config import SCOUT_CONFIG
from scout.settings_schema import default_scout_settings, effective_pattern_config
from utils import now_ist


def _cfg(settings: Optional[dict] = None) -> dict:
    if settings is not None:
        return effective_pattern_config(settings)
    return dict(SCOUT_CONFIG)

_VALID = "ACTIVE"
_EXPIRED = "EXPIRED"
_INVALIDATED = "INVALIDATED"
_OUT_OF_RANGE = "OUT_OF_RANGE"


def _market_close_dt(day: datetime) -> datetime:
    h, m = SCOUT_CONFIG.get("market_close", (15, 30))
    return day.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def _square_off_dt(day: datetime, settings: Optional[dict] = None) -> datetime:
    from scout.settings_schema import default_scout_settings, square_off_datetime
    return square_off_datetime(day, settings or default_scout_settings())


def _structural_target(signal: dict, meta: dict, action: str) -> Optional[float]:
    """Pattern-based measured-move target (OR/box extension), when levels exist."""
    st = str(signal.get("signal_type") or "").upper()
    action = action.upper()
    try:
        if st == "OR_BREAK_UP" and action == "BUY":
            or_h = float(meta["or_high"])
            or_l = float(meta["or_low"])
            return round(or_h + (or_h - or_l), 2)
        if st == "OR_BREAK_DOWN" and action == "SELL":
            or_h = float(meta["or_high"])
            or_l = float(meta["or_low"])
            return round(or_l - (or_h - or_l), 2)
        if st == "COMPRESSION_BREAK_UP" and action == "BUY":
            bh = float(meta["box_high"])
            bl = float(meta["box_low"])
            return round(bh + (bh - bl), 2)
        if st == "COMPRESSION_BREAK_DOWN" and action == "SELL":
            bh = float(meta["box_high"])
            bl = float(meta["box_low"])
            return round(bl - (bh - bl), 2)
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _entry_band(ltp: float, action: str, settings: Optional[dict] = None) -> tuple[float, float]:
    cfg = _cfg(settings)
    slip = float(cfg.get("entry_slippage_pct", 0.20)) / 100.0
    px = float(ltp)
    if str(action).upper() == "SELL":
        return round(px * (1.0 - slip * 1.5), 2), round(px * (1.0 + slip), 2)
    return round(px * (1.0 - slip), 2), round(px * (1.0 + slip * 1.5), 2)


def _setup_label(signal_type: Optional[str]) -> str:
    st = str(signal_type or "").replace("_", " ").strip()
    if not st:
        return "Intraday pattern"
    return st.title()


def _setup_code(signal_type: Optional[str]) -> str:
    codes = {
        "OR_BREAK_UP": "OR ↑",
        "OR_BREAK_DOWN": "OR ↓",
        "COMPRESSION_BREAK_UP": "BOX ↑",
        "COMPRESSION_BREAK_DOWN": "BOX ↓",
        "PULLBACK_UP": "PB ↑",
        "PULLBACK_DOWN": "PB ↓",
    }
    return codes.get(str(signal_type or "").upper(), "SETUP")


def _fmt_pct(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return None


def _build_dashboard(
    signal: dict,
    meta: dict,
    *,
    entry_min: float,
    entry_max: float,
    valid_until: datetime,
    now: datetime,
    action: str,
    live_ltp: Optional[float],
) -> dict:
    """Structured metrics for at-a-glance UI (numbers, codes — minimal prose)."""
    ltp = float(signal.get("ltp") or 0)
    ref_px = live_ltp if live_ltp is not None and live_ltp > 0 else ltp
    inv = signal.get("invalidation")

    stats: List[dict] = []
    stock_pct = meta.get("stock_pct_from_open")
    nifty_pct = meta.get("nifty_pct_from_open")
    if stock_pct is not None:
        stats.append({"key": "stock", "label": "Stock", "value": _fmt_pct(stock_pct), "raw": float(stock_pct)})
    if nifty_pct is not None:
        stats.append({"key": "nifty", "label": "Nifty", "value": _fmt_pct(nifty_pct), "raw": float(nifty_pct)})
    if stock_pct is not None and nifty_pct is not None:
        rs = round(float(stock_pct) - float(nifty_pct), 3)
        stats.append({"key": "rs", "label": "RS", "value": _fmt_pct(rs), "raw": rs})

    levels = None
    if meta.get("or_high") is not None:
        levels = {
            "kind": "OR",
            "low": round(float(meta["or_low"]), 2),
            "high": round(float(meta["or_high"]), 2),
        }
    elif meta.get("box_high") is not None:
        levels = {
            "kind": "BOX",
            "low": round(float(meta["box_low"]), 2),
            "high": round(float(meta["box_high"]), 2),
            "range_pct": meta.get("range_pct"),
        }

    stop_dist = None
    if inv is not None and ref_px > 0:
        inv_f = float(inv)
        if action == "BUY":
            dist_rs = ref_px - inv_f
        else:
            dist_rs = inv_f - ref_px
        stop_dist = {
            "rs": round(dist_rs, 2),
            "pct": round(dist_rs / ref_px * 100.0, 2),
        }

    secs_left = max(0, int((valid_until - now).total_seconds()))
    band_ok = ref_px >= entry_min and ref_px <= entry_max if ref_px > 0 else None
    stop_ok = None
    if inv is not None and ref_px > 0:
        inv_f = float(inv)
        stop_ok = ref_px >= inv_f if action == "BUY" else ref_px <= inv_f

    return {
        "setup_code": _setup_code(signal.get("signal_type")),
        "setup_type": str(signal.get("signal_type") or ""),
        "prices": {
            "live": live_ltp,
            "trigger": ltp,
            "band_lo": entry_min,
            "band_hi": entry_max,
            "stop": float(inv) if inv is not None else None,
        },
        "stats": stats,
        "levels": levels,
        "move_from_open_pct": meta.get("move_from_open_pct"),
        "timer_secs": secs_left,
        "timer_until": valid_until.strftime("%H:%M"),
        "stop_dist": stop_dist,
        "gates": {
            "band_ok": band_ok,
            "time_ok": secs_left > 0,
            "stop_ok": stop_ok,
        },
    }


def _build_conditions(
    signal: dict,
    meta: dict,
    *,
    entry_min: float,
    entry_max: float,
    valid_until: datetime,
    action: str,
) -> List[dict]:
    """Short checklist rows: {id, label, value, dynamic?}."""
    band_label = "Buy between" if action == "BUY" else "Sell between"
    items: List[dict] = [
        {"id": "session", "label": "Session", "value": "09:15–15:30 IST"},
        {"id": "setup", "label": "Setup", "value": _setup_label(signal.get("signal_type"))},
    ]

    stock_pct = meta.get("stock_pct_from_open")
    nifty_pct = meta.get("nifty_pct_from_open")
    if stock_pct is not None and nifty_pct is not None:
        items.append({
            "id": "rs",
            "label": "vs Nifty",
            "value": f"stock {stock_pct:+.1f}% · Nifty {nifty_pct:+.1f}%",
        })

    if meta.get("or_high") is not None:
        items.append({
            "id": "or",
            "label": "OR range",
            "value": f"{float(meta['or_low']):.0f} – {float(meta['or_high']):.0f}",
        })
    if meta.get("box_high") is not None:
        items.append({
            "id": "box",
            "label": "Box",
            "value": f"{float(meta['box_low']):.0f} – {float(meta['box_high']):.0f}",
        })

    items.append({
        "id": "band",
        "label": band_label,
        "value": f"₹{entry_min:.2f} – ₹{entry_max:.2f}",
        "dynamic": True,
    })
    items.append({
        "id": "window",
        "label": "Valid until",
        "value": valid_until.strftime("%H:%M IST"),
        "dynamic": True,
    })

    inv = signal.get("invalidation")
    if inv is not None:
        side = "below" if action == "BUY" else "above"
        items.append({
            "id": "stop",
            "label": "Stop if",
            "value": f"{side} ₹{float(inv):.2f}",
            "dynamic": True,
        })

    return items


def _parse_triggered(signal: dict, now: datetime) -> datetime:
    raw = signal.get("triggered_at")
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    return now


def evaluate_signal_status(
    signal: dict,
    *,
    live_ltp: Optional[float] = None,
    now: Optional[datetime] = None,
    settings: Optional[dict] = None,
) -> str:
    now = (now or now_ist()).replace(tzinfo=None)
    triggered = _parse_triggered(signal, now)
    cfg = _cfg(settings)
    valid_mins = int(cfg.get("signal_valid_minutes", 30))
    valid_until = min(triggered + timedelta(minutes=valid_mins), _market_close_dt(triggered))

    if now > valid_until:
        return _EXPIRED

    inv = signal.get("invalidation")
    action = str(signal.get("action") or "").upper()
    ltp = live_ltp
    if ltp is None:
        ltp = float(signal.get("ltp") or 0)

    if inv is not None and ltp > 0:
        inv_f = float(inv)
        if action == "BUY" and ltp < inv_f:
            return _INVALIDATED
        if action == "SELL" and ltp > inv_f:
            return _INVALIDATED

    entry_min, entry_max = _entry_band(float(signal.get("ltp") or 0), action, settings)
    if ltp > 0 and (ltp < entry_min or ltp > entry_max):
        return _OUT_OF_RANGE

    return _VALID


def enrich_signal(
    signal: dict,
    *,
    live_ltp: Optional[float] = None,
    live_as_of: Optional[str] = None,
    now: Optional[datetime] = None,
    settings: Optional[dict] = None,
) -> dict:
    """Add trade plan + validity fields; does not mutate input."""
    now = (now or now_ist()).replace(tzinfo=None)
    meta = dict(signal.get("meta") or {})
    action = str(signal.get("action") or "").upper()
    ltp_signal = float(signal.get("ltp") or 0)
    entry_min, entry_max = _entry_band(ltp_signal, action, settings)
    triggered = _parse_triggered(signal, now)
    cfg = _cfg(settings)
    valid_mins = int(cfg.get("signal_valid_minutes", 30))
    valid_until = min(triggered + timedelta(minutes=valid_mins), _market_close_dt(triggered))

    inv = signal.get("invalidation")
    invalidation_side = None
    if inv is not None:
        invalidation_side = "below" if action == "BUY" else "above"

    status = evaluate_signal_status(signal, live_ltp=live_ltp, now=now, settings=settings)
    live = live_ltp if live_ltp is not None else None

    out = dict(signal)
    out["entry_min"] = entry_min
    out["entry_max"] = entry_max
    out["valid_from"] = triggered.isoformat(sep=" ", timespec="seconds")
    out["valid_until"] = valid_until.isoformat(sep=" ", timespec="seconds")
    out["valid_minutes"] = valid_mins
    out["conditions"] = _build_conditions(
        signal,
        meta,
        entry_min=entry_min,
        entry_max=entry_max,
        valid_until=valid_until,
        action=action,
    )
    out["dashboard"] = _build_dashboard(
        signal,
        meta,
        entry_min=entry_min,
        entry_max=entry_max,
        valid_until=valid_until,
        now=now,
        action=action,
        live_ltp=live,
    )
    out["invalidation_side"] = invalidation_side
    out["live_ltp"] = live
    out["live_as_of"] = live_as_of
    out["validity_status"] = status
    out["is_actionable"] = status == _VALID and not signal.get("trade_open")
    return out


def build_exit_plan(
    signal: dict,
    *,
    entry_price: float,
    executed_at: Any = None,
    live_ltp: Optional[float] = None,
    now: Optional[datetime] = None,
    settings: Optional[dict] = None,
) -> dict:
    """Intraday exit guidance from fill: stop (invalidation), R-multiple target, square-off time."""
    now = (now or now_ist()).replace(tzinfo=None)
    action = str(signal.get("action") or "").upper()
    entry = float(entry_price or 0)
    inv = signal.get("invalidation")
    meta = dict(signal.get("meta") or {})
    square_off = _square_off_dt(now, settings)
    cfg = settings or default_scout_settings()
    r_mult = float(cfg.get("min_target_r") or SCOUT_CONFIG.get("target_r_multiple", 2.0))
    max_r = float(SCOUT_CONFIG.get("target_max_r_multiple", 2.5))
    breakeven_at_r = float(cfg.get("breakeven_at_r", 1.0))

    stop_price = float(inv) if inv is not None else None
    stop_side = "below" if action == "BUY" else "above"
    structural = _structural_target(signal, meta, action)

    risk = abs(entry - stop_price) if stop_price is not None and entry > 0 else None
    target_price: Optional[float] = None
    if risk and risk > 0 and entry > 0:
        if action == "BUY":
            r_target = round(entry + risk * r_mult, 2)
            cap = round(entry + risk * max_r, 2)
            target_price = r_target
            if structural is not None and structural >= r_target and structural <= cap:
                target_price = structural
            elif structural is not None and structural > cap:
                target_price = cap
        else:
            r_target = round(entry - risk * r_mult, 2)
            cap = round(entry - risk * max_r, 2)
            target_price = r_target
            if structural is not None and structural <= r_target and structural >= cap:
                target_price = structural
            elif structural is not None and structural < cap:
                target_price = cap

    reward = abs(target_price - entry) if target_price is not None and entry > 0 else None
    ref_px = live_ltp if live_ltp is not None and live_ltp > 0 else entry

    target_dist = stop_dist = None
    if target_price is not None and ref_px > 0:
        dist = (target_price - ref_px) if action == "BUY" else (ref_px - target_price)
        target_dist = {"rs": round(dist, 2), "pct": round(dist / ref_px * 100.0, 2)}
    if stop_price is not None and ref_px > 0:
        dist = (ref_px - stop_price) if action == "BUY" else (stop_price - ref_px)
        stop_dist = {"rs": round(dist, 2), "pct": round(dist / ref_px * 100.0, 2)}

    secs_left = max(0, int((square_off - now).total_seconds()))
    conditions: List[dict] = [
        {
            "id": "target",
            "label": "Target",
            "value": (f"₹{target_price:.2f} ({r_mult}R)" if target_price is not None else " —"),
            "dynamic": True,
        },
        {
            "id": "exit_time",
            "label": "Square off by",
            "value": square_off.strftime("%H:%M IST"),
            "dynamic": True,
        },
    ]
    if stop_price is not None:
        conditions.insert(1, {
            "id": "stop",
            "label": "Stop if",
            "value": f"{stop_side} ₹{stop_price:.2f}",
            "dynamic": True,
        })
    if structural is not None and structural != target_price:
        conditions.append({
            "id": "struct",
            "label": "Measured move",
            "value": f"₹{structural:.2f}",
        })

    return {
        "stop_price": stop_price,
        "stop_side": stop_side,
        "target_price": target_price,
        "target_r": r_mult,
        "breakeven_at_r": breakeven_at_r,
        "risk_per_share": round(risk, 2) if risk else None,
        "reward_per_share": round(reward, 2) if reward else None,
        "structural_target": structural,
        "square_off_by": square_off.strftime("%H:%M IST"),
        "square_off_at": square_off.isoformat(sep=" ", timespec="seconds"),
        "conditions": conditions,
        "dashboard": {
            "prices": {
                "entry": round(entry, 2) if entry > 0 else None,
                "stop": stop_price,
                "target": target_price,
                "live": live_ltp,
            },
            "target_dist": target_dist,
            "stop_dist": stop_dist,
            "timer_secs": secs_left,
            "timer_until": square_off.strftime("%H:%M"),
            "target_r": r_mult,
            "breakeven_at_r": breakeven_at_r,
        },
    }


def _effective_stop(
    *,
    action: str,
    entry: float,
    original_stop: float,
    live_ltp: float,
    risk: float,
    peak_price: Optional[float],
    settings: Optional[dict],
) -> tuple[float, bool]:
    """Return (effective_stop, breakeven_armed)."""
    cfg = settings or default_scout_settings()
    be_r = float(cfg.get("breakeven_at_r", 1.0))
    trail_frac = float(cfg.get("trail_stop_r_fraction", 0.5))
    action_u = str(action or "").upper()
    armed = False
    if risk <= 0:
        return original_stop, armed
    ref_peak = float(peak_price) if peak_price is not None else live_ltp
    if action_u == "BUY":
        effective = original_stop
        if ref_peak >= entry + be_r * risk:
            armed = True
            effective = max(effective, entry)
        if peak_price is not None and trail_frac > 0 and ref_peak >= entry + be_r * risk:
            trail = float(peak_price) - trail_frac * risk
            effective = max(effective, trail)
        return effective, armed
    effective = original_stop
    if ref_peak <= entry - be_r * risk:
        armed = True
        effective = min(effective, entry)
    if peak_price is not None and trail_frac > 0 and ref_peak <= entry - be_r * risk:
        trail = float(peak_price) + trail_frac * risk
        effective = min(effective, trail)
    return effective, armed


def evaluate_exit_alerts(
    *,
    action: str,
    live_ltp: Optional[float],
    exit_plan: dict,
    entry_price: Optional[float] = None,
    peak_price: Optional[float] = None,
    settings: Optional[dict] = None,
) -> dict:
    """Flag when target, stop, or square-off time requires closing the open trade."""
    action = str(action or "").upper()
    dash = dict(exit_plan.get("dashboard") or {})
    prices = dict(dash.get("prices") or {})
    target = prices.get("target")
    stop = prices.get("stop")
    entry = float(entry_price if entry_price is not None else (prices.get("entry") or 0))
    risk = exit_plan.get("risk_per_share")
    risk_f = float(risk) if risk is not None else None
    timer_secs = int(dash.get("timer_secs") or 0)
    warn_mins = int((settings or default_scout_settings()).get("square_off_warn_minutes", 5))
    warn_secs = max(0, warn_mins * 60)

    flags = {
        "target_hit": False,
        "stop_hit": False,
        "square_off_due": False,
        "square_off_soon": False,
        "breakeven_armed": False,
    }
    alerts: List[dict] = []

    ltp = float(live_ltp) if live_ltp is not None and float(live_ltp) > 0 else None
    if ltp is not None:
        if target is not None:
            tgt = float(target)
            if action == "BUY" and ltp >= tgt:
                flags["target_hit"] = True
                alerts.append({
                    "code": "TARGET_HIT",
                    "level": "now",
                    "label": f"Target hit (LTP ₹{ltp:.2f} ≥ ₹{tgt:.2f})",
                })
            elif action == "SELL" and ltp <= tgt:
                flags["target_hit"] = True
                alerts.append({
                    "code": "TARGET_HIT",
                    "level": "now",
                    "label": f"Target hit (LTP ₹{ltp:.2f} ≤ ₹{tgt:.2f})",
                })
        if stop is not None and entry > 0 and risk_f and risk_f > 0:
            orig = float(stop)
            stp, armed = _effective_stop(
                action=action,
                entry=entry,
                original_stop=orig,
                live_ltp=ltp,
                risk=risk_f,
                peak_price=peak_price,
                settings=settings,
            )
            flags["breakeven_armed"] = armed
            if action == "BUY" and ltp <= stp:
                flags["stop_hit"] = True
                label = f"Stop hit (LTP ₹{ltp:.2f} ≤ ₹{stp:.2f})"
                if flags["breakeven_armed"]:
                    label = f"Trail/breakeven stop (LTP ₹{ltp:.2f} ≤ ₹{stp:.2f})"
                alerts.append({"code": "STOP_HIT", "level": "now", "label": label})
            elif action == "SELL" and ltp >= stp:
                flags["stop_hit"] = True
                label = f"Stop hit (LTP ₹{ltp:.2f} ≥ ₹{stp:.2f})"
                if flags["breakeven_armed"]:
                    label = f"Trail/breakeven stop (LTP ₹{ltp:.2f} ≥ ₹{stp:.2f})"
                alerts.append({"code": "STOP_HIT", "level": "now", "label": label})
        elif stop is not None:
            stp = float(stop)
            if action == "BUY" and ltp <= stp:
                flags["stop_hit"] = True
                alerts.append({
                    "code": "STOP_HIT",
                    "level": "now",
                    "label": f"Stop hit (LTP ₹{ltp:.2f} ≤ ₹{stp:.2f})",
                })
            elif action == "SELL" and ltp >= stp:
                flags["stop_hit"] = True
                alerts.append({
                    "code": "STOP_HIT",
                    "level": "now",
                    "label": f"Stop hit (LTP ₹{ltp:.2f} ≥ ₹{stp:.2f})",
                })

    if timer_secs <= 0:
        flags["square_off_due"] = True
        alerts.append({
            "code": "SQUARE_OFF_DUE",
            "level": "now",
            "label": f"Square-off time ({exit_plan.get('square_off_by') or 'now'})",
        })
    elif timer_secs <= warn_secs:
        flags["square_off_soon"] = True
        mins = max(1, int(timer_secs / 60))
        alerts.append({
            "code": "SQUARE_OFF_SOON",
            "level": "warn",
            "label": f"Square-off in {mins}m ({exit_plan.get('square_off_by') or ''})",
        })

    urgency = "none"
    if any(a.get("level") == "now" for a in alerts):
        urgency = "now"
    elif alerts:
        urgency = "warn"

    return {
        "alerts": alerts,
        "urgency": urgency,
        "flags": flags,
        "close_now": urgency == "now",
    }


def scout_trade_mtm(trade: dict, live_ltp: Optional[float]) -> Optional[dict]:
    if live_ltp is None or live_ltp <= 0:
        return None
    entry = float(trade.get("entry_price") or 0)
    try:
        qty = max(1, int(float(trade.get("quantity") or 1)))
    except (TypeError, ValueError):
        qty = 1
    if entry <= 0:
        return None
    action = str(trade.get("action") or "").upper()
    ltp = float(live_ltp)
    if action == "BUY":
        pnl_per_share = ltp - entry
    else:
        pnl_per_share = entry - ltp
    pnl = pnl_per_share * qty
    notional = entry * qty
    pct = (pnl / notional) * 100.0 if notional else 0.0

    from engine.equity_charges import estimate_equity_intraday_charges

    charges = estimate_equity_intraday_charges(
        entry=entry, exit_px=ltp, qty=qty,
    ).total
    net = round(pnl - charges, 2)

    return {
        "live_ltp": ltp,
        "quantity": qty,
        "mtm": round(pnl, 2),
        "mtm_net": net,
        "total_charges": round(charges, 2),
        "mtm_pct": round(pct, 2),
        "mtm_per_share": round(pnl_per_share, 2),
        "position_value": round(ltp * qty, 2),
        "entry_value": round(notional, 2),
    }
