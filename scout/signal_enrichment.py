"""Enrich scout signals with entry band, validity, conditions, and live status."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from config import SCOUT_CONFIG
from utils import now_ist

_VALID = "ACTIVE"
_EXPIRED = "EXPIRED"
_INVALIDATED = "INVALIDATED"
_OUT_OF_RANGE = "OUT_OF_RANGE"


def _market_close_dt(day: datetime) -> datetime:
    h, m = SCOUT_CONFIG.get("market_close", (15, 30))
    return day.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def _square_off_dt(day: datetime) -> datetime:
    close = _market_close_dt(day)
    mins = int(SCOUT_CONFIG.get("square_off_minutes_before_close", 15))
    return close - timedelta(minutes=max(0, mins))


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


def _entry_band(ltp: float, action: str) -> tuple[float, float]:
    slip = float(SCOUT_CONFIG.get("entry_slippage_pct", 0.20)) / 100.0
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
) -> str:
    now = (now or now_ist()).replace(tzinfo=None)
    triggered = _parse_triggered(signal, now)
    valid_mins = int(SCOUT_CONFIG.get("signal_valid_minutes", 30))
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

    entry_min, entry_max = _entry_band(float(signal.get("ltp") or 0), action)
    if ltp > 0 and (ltp < entry_min or ltp > entry_max):
        return _OUT_OF_RANGE

    return _VALID


def enrich_signal(
    signal: dict,
    *,
    live_ltp: Optional[float] = None,
    live_as_of: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Add trade plan + validity fields; does not mutate input."""
    now = (now or now_ist()).replace(tzinfo=None)
    meta = dict(signal.get("meta") or {})
    action = str(signal.get("action") or "").upper()
    ltp_signal = float(signal.get("ltp") or 0)
    entry_min, entry_max = _entry_band(ltp_signal, action)
    triggered = _parse_triggered(signal, now)
    valid_mins = int(SCOUT_CONFIG.get("signal_valid_minutes", 30))
    valid_until = min(triggered + timedelta(minutes=valid_mins), _market_close_dt(triggered))

    inv = signal.get("invalidation")
    invalidation_side = None
    if inv is not None:
        invalidation_side = "below" if action == "BUY" else "above"

    status = evaluate_signal_status(signal, live_ltp=live_ltp, now=now)
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
) -> dict:
    """Intraday exit guidance from fill: stop (invalidation), R-multiple target, square-off time."""
    now = (now or now_ist()).replace(tzinfo=None)
    action = str(signal.get("action") or "").upper()
    entry = float(entry_price or 0)
    inv = signal.get("invalidation")
    meta = dict(signal.get("meta") or {})
    square_off = _square_off_dt(now)
    r_mult = float(SCOUT_CONFIG.get("target_r_multiple", 1.5))
    max_r = float(SCOUT_CONFIG.get("target_max_r_multiple", 2.5))

    stop_price = float(inv) if inv is not None else None
    stop_side = "below" if action == "BUY" else "above"
    structural = _structural_target(signal, meta, action)

    risk = abs(entry - stop_price) if stop_price is not None and entry > 0 else None
    target_price: Optional[float] = None
    if risk and risk > 0 and entry > 0:
        if action == "BUY":
            r_target = round(entry + risk * r_mult, 2)
            cap = round(entry + risk * max_r, 2)
            if structural is not None and entry < structural <= cap:
                target_price = structural
            else:
                target_price = r_target
        else:
            r_target = round(entry - risk * r_mult, 2)
            cap = round(entry - risk * max_r, 2)
            if structural is not None and entry > structural >= cap:
                target_price = structural
            else:
                target_price = r_target

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
            "value": f"₹{target_price:.2f}" + (f" ({r_mult}R)" if target_price else ""),
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
        },
    }


def scout_trade_mtm(trade: dict, live_ltp: Optional[float]) -> Optional[dict]:
    if live_ltp is None or live_ltp <= 0:
        return None
    entry = float(trade.get("entry_price") or 0)
    qty = int(trade.get("quantity") or 1)
    if entry <= 0 or qty <= 0:
        return None
    action = str(trade.get("action") or "").upper()
    if action == "BUY":
        pnl = (live_ltp - entry) * qty
    else:
        pnl = (entry - live_ltp) * qty
    pct = (pnl / (entry * qty)) * 100.0 if entry else 0.0
    return {
        "live_ltp": live_ltp,
        "mtm": round(pnl, 2),
        "mtm_pct": round(pct, 2),
    }
