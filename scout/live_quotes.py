"""Live NSE equity LTP from ws_status.json (written by ws_runner)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional

from utils import now_ist

logger = logging.getLogger(__name__)

_INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "VIX", "MIDCPNIFTY"})
_DEFAULT_MAX_AGE_SECONDS = 45


def _snapshot_path() -> Path:
    from providers.ws_monitor import default_snapshot_path
    return default_snapshot_path()


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _quote_age_seconds(as_of: Optional[str], *, now: Optional[datetime] = None) -> Optional[float]:
    ts = _parse_ts(as_of)
    if ts is None:
        return None
    ref = (now or now_ist()).replace(tzinfo=None)
    return max(0.0, (ref - ts).total_seconds())


def latest_equity_ltps(
    symbols: Optional[Iterable[str]] = None,
    *,
    max_age_seconds: Optional[int] = _DEFAULT_MAX_AGE_SECONDS,
) -> Dict[str, dict]:
    """Return {SYMBOL: {ltp, as_of, stale?}} from recent equity ticks in ws_status."""
    want = {str(s).upper() for s in symbols} if symbols else None
    path = _snapshot_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("scout live quotes: read failed: %s", exc)
        return {}

    now = now_ist().replace(tzinfo=None)
    snap_age = _quote_age_seconds(snap.get("generated_at"), now=now)
    if max_age_seconds is not None and snap_age is not None and snap_age > max_age_seconds:
        return {}

    out: Dict[str, dict] = {}
    events = snap.get("recent_events") or []
    for ev in reversed(events):
        topic = str(ev.get("topic") or "")
        if topic and topic not in ("tick.scout", "tick"):
            continue
        if topic == "tick" and ev.get("option_type"):
            continue
        sym = str(ev.get("symbol") or "").upper()
        if not sym or sym in _INDEX_SYMBOLS:
            continue
        if ev.get("option_type") is not None:
            continue
        if want is not None and sym not in want:
            continue
        if sym in out:
            continue
        px = ev.get("last_price")
        if px is None:
            continue
        try:
            ltp = float(px)
        except (TypeError, ValueError):
            continue
        if ltp <= 0:
            continue
        as_of = ev.get("ts")
        age = _quote_age_seconds(as_of, now=now)
        if max_age_seconds is not None and age is not None and age > max_age_seconds:
            continue
        out[sym] = {
            "ltp": ltp,
            "as_of": as_of,
            "age_seconds": round(age, 1) if age is not None else None,
        }
    return out


def fresh_equity_ltp(symbol: str, *, max_age_seconds: Optional[int] = _DEFAULT_MAX_AGE_SECONDS) -> Optional[float]:
    """Return a non-stale LTP for one symbol, or None when quote is missing/old."""
    q = latest_equity_ltps([symbol], max_age_seconds=max_age_seconds).get(str(symbol).upper())
    if not q:
        return None
    ltp = q.get("ltp")
    return float(ltp) if ltp is not None and float(ltp) > 0 else None
