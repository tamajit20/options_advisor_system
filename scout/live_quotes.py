"""Live NSE equity LTP from ws_status.json (written by ws_runner)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

_INDEX_SYMBOLS = frozenset({"NIFTY", "BANKNIFTY", "FINNIFTY", "VIX", "MIDCPNIFTY"})


def _snapshot_path() -> Path:
    from providers.ws_monitor import default_snapshot_path
    return default_snapshot_path()


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def latest_equity_ltps(symbols: Optional[Iterable[str]] = None) -> Dict[str, dict]:
    """Return {SYMBOL: {ltp, as_of}} from the most recent equity ticks in ws_status."""
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
        out[sym] = {"ltp": ltp, "as_of": ev.get("ts")}
    return out
