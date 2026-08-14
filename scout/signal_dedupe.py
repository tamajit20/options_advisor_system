"""Shared signal dedupe keys — memory + DB must use the same rules."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional


def signal_dedupe_key(symbol: str, signal_type: str, *, dedupe_per_symbol: bool) -> str:
    sym = str(symbol).upper()
    if dedupe_per_symbol:
        return sym
    return f"{sym}:{str(signal_type).upper()}"


def is_within_dedupe_window(
    previous: Optional[datetime],
    at: datetime,
    dedupe_minutes: int,
) -> bool:
    if previous is None:
        return False
    mins = max(1, int(dedupe_minutes))
    return (at - previous).total_seconds() < mins * 60


def build_dedupe_cache(
    rows: list,
    *,
    dedupe_per_symbol: bool,
) -> Dict[str, datetime]:
    """Build in-memory dedupe map from DB rows (symbol, signal_type, triggered_at)."""
    out: Dict[str, datetime] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        triggered = row.get("triggered_at")
        if not isinstance(triggered, datetime):
            continue
        key = signal_dedupe_key(
            sym,
            str(row.get("signal_type") or ""),
            dedupe_per_symbol=dedupe_per_symbol,
        )
        prev = out.get(key)
        if prev is None or triggered > prev:
            out[key] = triggered
    return out
