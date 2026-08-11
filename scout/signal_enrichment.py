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


def _entry_band(ltp: float, action: str) -> tuple[float, float]:
    slip = float(SCOUT_CONFIG.get("entry_slippage_pct", 0.20)) / 100.0
    px = float(ltp)
    if str(action).upper() == "SELL":
        return round(px * (1.0 - slip * 1.5), 2), round(px * (1.0 + slip), 2)
    return round(px * (1.0 - slip), 2), round(px * (1.0 + slip * 1.5), 2)


def _build_conditions(signal: dict, meta: dict) -> List[str]:
    conditions: List[str] = []
    st = str(signal.get("signal_type") or "")
    action = str(signal.get("action") or "").upper()

    conditions.append("Intraday market session (09:15–15:30 IST)")
    conditions.append(f"Pattern: {st.replace('_', ' ').title()}")

    stock_pct = meta.get("stock_pct_from_open")
    nifty_pct = meta.get("nifty_pct_from_open")
    if stock_pct is not None and nifty_pct is not None:
        conditions.append(
            f"Relative strength vs Nifty OK ({action}: stock {stock_pct:+.2f}% vs Nifty {nifty_pct:+.2f}%)"
        )

    if meta.get("or_high") is not None:
        conditions.append(
            f"Opening range {meta.get('or_low'):.2f} – {meta.get('or_high'):.2f}"
        )
    if meta.get("box_high") is not None:
        conditions.append(
            f"Compression box {meta.get('box_low'):.2f} – {meta.get('box_high'):.2f}"
        )

    conditions.append("Enter within the price band before the validity window ends")
    if signal.get("invalidation") is not None:
        side = "below" if action == "BUY" else "above"
        conditions.append(
            f"Abort if price closes {side} invalidation ({float(signal['invalidation']):.2f})"
        )
    return conditions


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
    out["conditions"] = _build_conditions(signal, meta)
    out["invalidation_side"] = invalidation_side
    out["live_ltp"] = live
    out["live_as_of"] = live_as_of
    out["validity_status"] = status
    out["is_actionable"] = status == _VALID and not signal.get("trade_open")
    return out


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
