"""Warn when a new suggestion reuses an option an open trade still holds.

Zerodha nets by contract. A second order on the same symbol, expiry, strike,
and option type changes the position the first trade thinks it owns. This is
a warning only — it does not block Place.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


def _expiry_text(expiry: Any) -> str:
    if expiry is None:
        return ""
    if hasattr(expiry, "isoformat"):
        return str(expiry.isoformat())[:10]
    return str(expiry)[:10]


def _strike_text(strike: Any) -> str:
    try:
        n = float(strike)
    except (TypeError, ValueError):
        return str(strike or "").strip()
    if abs(n - round(n)) < 1e-6:
        return str(int(round(n)))
    return f"{n:.4f}".rstrip("0").rstrip(".")


def contract_key(symbol: Any, expiry: Any, strike: Any, option_type: Any) -> tuple:
    return (
        str(symbol or "").strip().upper(),
        _expiry_text(expiry),
        _strike_text(strike),
        str(option_type or "").strip().upper(),
    )


def instrument_label(symbol: Any, expiry: Any, strike: Any, option_type: Any) -> str:
    parts = [
        str(symbol or "").strip().upper(),
        _expiry_text(expiry),
        _strike_text(strike),
        str(option_type or "").strip().upper(),
    ]
    return " ".join(p for p in parts if p)


def load_open_contracts(db) -> list[dict]:
    """Filled, not-yet-exited legs on ACTIVE trades."""
    try:
        rows = db.fetch_all(
            """
            SELECT t.trade_id, t.trade_name, t.suggestion_id,
                   sl.symbol, sl.expiry_date, sl.strike, sl.option_type, sl.action
            FROM options_trades t
            JOIN options_trade_legs tl ON tl.trade_id = t.trade_id
            JOIN options_suggestion_legs sl ON sl.id = tl.suggestion_leg_id
            WHERE t.status = 'ACTIVE'
              AND tl.executed = 1
              AND tl.exit_time IS NULL
            """
        )
    except Exception:
        logger.exception("open instrument lookup failed")
        return []
    if not isinstance(rows, list):
        return []
    return rows


def overlaps_for_legs(
    open_rows: Iterable[dict],
    suggestion_legs: Iterable[dict],
    *,
    exclude_suggestion_id: Optional[str] = None,
) -> list[dict]:
    """Contracts on this suggestion that an open trade still holds.

    A trade created from this same suggestion is skipped so the card does not
    warn about itself.
    """
    index: dict[tuple, list[dict]] = {}
    for row in open_rows or []:
        if not isinstance(row, dict):
            continue
        if exclude_suggestion_id and str(row.get("suggestion_id") or "") == str(exclude_suggestion_id):
            continue
        key = contract_key(
            row.get("symbol"), row.get("expiry_date"),
            row.get("strike"), row.get("option_type"),
        )
        if not key[0] or not key[3]:
            continue
        index.setdefault(key, []).append(row)

    hits: list[dict] = []
    seen: set[tuple] = set()
    for leg in suggestion_legs or []:
        if not isinstance(leg, dict):
            continue
        key = contract_key(
            leg.get("symbol"), leg.get("expiry_date"),
            leg.get("strike"), leg.get("option_type"),
        )
        for row in index.get(key, []):
            ident = (str(row.get("trade_id") or ""), key)
            if ident in seen:
                continue
            seen.add(ident)
            label = instrument_label(*key)
            hits.append({
                "trade_id": row.get("trade_id"),
                "trade_name": row.get("trade_name"),
                "symbol": key[0],
                "expiry_date": key[1],
                "strike": key[2],
                "option_type": key[3],
                "open_action": row.get("action"),
                "suggestion_action": leg.get("action"),
                "label": label,
            })
    return hits
