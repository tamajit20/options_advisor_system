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
_STICKY_MAX_AGE_SECONDS = 300
_STICKY_QUOTES: Dict[str, dict] = {}


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


def _quote_entry(
    sym: str,
    ltp: float,
    as_of: Optional[str],
    *,
    now: datetime,
    max_age_seconds: Optional[int],
    source: str,
) -> Optional[dict]:
    if ltp <= 0:
        return None
    age = _quote_age_seconds(as_of, now=now)
    stale = False
    if max_age_seconds is not None and age is not None and age > max_age_seconds:
        stale = True
    return {
        "ltp": ltp,
        "as_of": as_of,
        "age_seconds": round(age, 1) if age is not None else None,
        "stale": stale,
        "source": source,
    }


def _read_last_equity_map(
    snap: dict,
    *,
    want: Optional[set[str]],
    now: datetime,
    max_age_seconds: Optional[int],
) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    raw = snap.get("last_equity_ltps") or {}
    if not isinstance(raw, dict):
        return out
    for sym_raw, entry in raw.items():
        sym = str(sym_raw or "").upper()
        if not sym or sym in _INDEX_SYMBOLS:
            continue
        if want is not None and sym not in want:
            continue
        if not isinstance(entry, dict):
            continue
        px = entry.get("ltp")
        if px is None:
            continue
        try:
            ltp = float(px)
        except (TypeError, ValueError):
            continue
        as_of = entry.get("as_of") or entry.get("ts")
        q = _quote_entry(sym, ltp, as_of, now=now, max_age_seconds=max_age_seconds, source="last_map")
        if q:
            out[sym] = q
    return out


def _read_recent_events(
    snap: dict,
    *,
    want: Optional[set[str]],
    now: datetime,
    max_age_seconds: Optional[int],
) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for ev in reversed(snap.get("recent_events") or []):
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
        as_of = ev.get("ts")
        q = _quote_entry(sym, ltp, as_of, now=now, max_age_seconds=max_age_seconds, source="events")
        if q and not q.get("stale"):
            out[sym] = q
    return out


def _apply_sticky(
    out: Dict[str, dict],
    *,
    want: Optional[set[str]],
    now: datetime,
) -> Dict[str, dict]:
    """Fill gaps from the in-process sticky cache; keep last known tick between polls."""
    targets = want if want is not None else set(out.keys()) | set(_STICKY_QUOTES.keys())
    for sym in targets:
        if sym in out:
            entry = dict(out[sym])
            entry["stale"] = bool(entry.get("stale"))
            _STICKY_QUOTES[sym] = entry
            out[sym] = entry
            continue
        cached = _STICKY_QUOTES.get(sym)
        if not cached:
            continue
        age = _quote_age_seconds(cached.get("as_of"), now=now)
        if age is not None and age > _STICKY_MAX_AGE_SECONDS:
            continue
        entry = dict(cached)
        entry["stale"] = True
        entry["source"] = "sticky"
        entry["age_seconds"] = round(age, 1) if age is not None else entry.get("age_seconds")
        out[sym] = entry
    return out


def _sticky_only(want: Optional[set[str]], *, now: datetime) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if want is None:
        return out
    for sym in want:
        cached = _STICKY_QUOTES.get(sym)
        if not cached:
            continue
        age = _quote_age_seconds(cached.get("as_of"), now=now)
        if age is not None and age > _STICKY_MAX_AGE_SECONDS:
            continue
        entry = dict(cached)
        entry["stale"] = True
        entry["source"] = "sticky"
        entry["age_seconds"] = round(age, 1) if age is not None else entry.get("age_seconds")
        out[sym] = entry
    return out


def latest_equity_ltps(
    symbols: Optional[Iterable[str]] = None,
    *,
    max_age_seconds: Optional[int] = _DEFAULT_MAX_AGE_SECONDS,
) -> Dict[str, dict]:
    """Return {SYMBOL: {ltp, as_of, stale?, age_seconds?}} from ws_status.

    Uses last_equity_ltps map, recent_events ring, and an in-process sticky cache
    so symbols do not disappear between tick intervals or when they fall out of
    the recent_events buffer.
    """
    want = {str(s).upper() for s in symbols} if symbols else None
    now = now_ist().replace(tzinfo=None)
    path = _snapshot_path()
    if not path.exists():
        return _sticky_only(want, now=now)

    try:
        with path.open("r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("scout live quotes: read failed: %s", exc)
        return _sticky_only(want, now=now)

    snap_age = _quote_age_seconds(snap.get("generated_at"), now=now)
    snap_stale = (
        max_age_seconds is not None
        and snap_age is not None
        and snap_age > max_age_seconds
    )

    out: Dict[str, dict] = _read_last_equity_map(
        snap, want=want, now=now, max_age_seconds=max_age_seconds,
    )
    if not snap_stale:
        for sym, q in _read_recent_events(
            snap, want=want, now=now, max_age_seconds=max_age_seconds,
        ).items():
            out[sym] = q

    if snap_stale:
        for sym in list(out.keys()):
            entry = dict(out[sym])
            entry["stale"] = True
            out[sym] = entry

    out = _apply_sticky(out, want=want, now=now)
    if want is not None:
        return {sym: out[sym] for sym in want if sym in out}
    return out


def fresh_equity_ltp(symbol: str, *, max_age_seconds: Optional[int] = _DEFAULT_MAX_AGE_SECONDS) -> Optional[float]:
    """Return LTP for one symbol when quote exists and is not marked stale."""
    q = latest_equity_ltps([symbol], max_age_seconds=max_age_seconds).get(str(symbol).upper())
    if not q or q.get("stale"):
        return None
    ltp = q.get("ltp")
    return float(ltp) if ltp is not None and float(ltp) > 0 else None
