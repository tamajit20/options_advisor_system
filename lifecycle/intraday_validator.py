"""
lifecycle/intraday_validator.py
===============================

09:35 IST opening-bell validator.

For each PENDING suggestion with `entry_date == today`, fetch the live
opening chain, re-price every leg, recompute the net credit, and decide
whether the suggestion is still valid.

Outcomes (stamped on `options_suggestions.validator_status`):

* `STILL_GOOD_0935`  — every leg priced; aggregate net credit within
                       ±`STRATEGY_CONFIG["intraday_validator_tolerance_pct"]`
                       of the originally suggested credit. Status stays
                       `PENDING`. INFO notification fired.
* `STALE_0935`       — at least one leg missing from the chain, OR the
                       net credit moved outside the tolerance band.
                       Status flipped to `IGNORED`. WARNING notification
                       fired so the user knows not to execute.

This is a **gate**, not a re-suggestion. We never write a new suggestion
here — that's the WS regen path. We only validate what we already
shipped overnight.

Locked architecture rules
-------------------------
* Read-only Zerodha — provider must be a `MarketDataProvider`; we call
  `get_chain(symbol, today, expiry)` only. No order or position reads.
* Fail-open on missing live data — when `get_chain` raises or returns
  empty, we mark the suggestion `NOT_VALIDATED` (no status flip) and log;
  the user still sees the morning suggestion. Better than silently
  killing it on a Zerodha outage.
* Idempotent — re-running for the same suggestion overwrites
  `validator_status` and emits a new notification (the dashboard's daily
  dedup keeps the noise down). Useful for retry after a transient
  provider failure.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from config import STRATEGY_CONFIG
from database.connection import SQLServerConnection
from database.models import SuggestionRepo
from notifications import Notifier, build_notifier
from providers.base import MarketDataProvider
from providers.registry import get_market_data
from utils import today_ist


logger = logging.getLogger(__name__)


# Validator status values (also written to options_suggestions.validator_status)
_STATUS_STILL_GOOD = "STILL_GOOD_0935"
_STATUS_STALE      = "STALE_0935"
_STATUS_NOT_VALID  = "NOT_VALIDATED"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _chain_index(rows: Iterable[dict]) -> Dict[Tuple[float, str], dict]:
    out: Dict[Tuple[float, str], dict] = {}
    for r in rows or []:
        try:
            key = (float(r["strike"]), str(r["option_type"]).upper())
        except (KeyError, ValueError, TypeError):
            continue
        out[key] = r
    return out


def _row_price(row: dict) -> Optional[float]:
    """Pick a usable mid/LTP from the chain row.

    Live providers populate `last_price`; EOD rows have `settle_price`/
    `close_price`. Returns None if no positive value is available.
    """
    for k in ("last_price", "settle_price", "close_price"):
        v = row.get(k)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def _set_validator_status(
    db: SQLServerConnection, suggestion_id: str, status: str
) -> None:
    """Write to the `validator_status` column added by the Phase 2c
    migration. Best-effort — column may not exist on a not-yet-migrated DB."""
    try:
        db.execute(
            "UPDATE options_suggestions SET validator_status = ? "
            "WHERE suggestion_id = ?",
            [status, suggestion_id],
        ).close()
    except Exception:
        logger.exception(
            "intraday_validator: failed to write validator_status=%s for %s",
            status, suggestion_id,
        )


# ---------------------------------------------------------------------------
# Per-suggestion evaluation
# ---------------------------------------------------------------------------
def _evaluate_one(
    db: SQLServerConnection,
    sug: dict,
    legs: List[dict],
    *,
    trade_date: date,
    provider: MarketDataProvider,
    tolerance_pct: float,
) -> Tuple[str, str]:
    """Return (validator_status, human_readable_summary) for a single suggestion.

    Does NOT update DB or fire notifications — the caller owns that side
    effect so we can keep this function trivially testable.
    """
    suggestion_id = sug["suggestion_id"]
    underlying = sug.get("underlying") or (legs[0]["symbol"] if legs else "?")
    expiry = sug.get("expiry_date") or (legs[0]["expiry_date"] if legs else None)
    if expiry is None:
        return _STATUS_NOT_VALID, "missing expiry"

    try:
        chain = provider.get_chain(underlying, trade_date, expiry)
    except Exception as exc:
        logger.warning(
            "intraday_validator: provider raised on %s/%s: %s",
            underlying, expiry, exc,
        )
        return _STATUS_NOT_VALID, f"provider error: {exc}"

    if not chain:
        return _STATUS_NOT_VALID, "empty chain returned"

    idx = _chain_index(chain)
    missing: List[str] = []
    current_credit_per_unit = 0.0
    for leg in legs:
        try:
            key = (float(leg["strike"]), str(leg["option_type"]).upper())
        except (KeyError, ValueError, TypeError):
            missing.append(f"leg{leg.get('leg_order')}")
            continue
        row = idx.get(key)
        price = _row_price(row) if row is not None else None
        if price is None:
            missing.append(f"{leg.get('strike')}{leg.get('option_type')}")
            continue
        sign = 1.0 if str(leg["action"]).upper() == "SELL" else -1.0
        current_credit_per_unit += sign * price

    if missing:
        return _STATUS_NOT_VALID, f"chain missing: {', '.join(missing)}"

    suggested_credit = sug.get("net_credit_suggested")
    if suggested_credit is None:
        return _STATUS_NOT_VALID, "suggestion has no net_credit_suggested"
    suggested_credit = float(suggested_credit)
    if abs(suggested_credit) < 1e-9:
        return _STATUS_NOT_VALID, "suggested net credit is zero"

    pct_change = (current_credit_per_unit - suggested_credit) / abs(suggested_credit) * 100.0
    summary = (
        f"net credit {current_credit_per_unit:+.2f} vs suggested "
        f"{suggested_credit:+.2f} (change {pct_change:+.2f}%, "
        f"tolerance ±{tolerance_pct:.1f}%)"
    )
    if abs(pct_change) <= tolerance_pct:
        return _STATUS_STILL_GOOD, summary
    return _STATUS_STALE, summary


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_intraday_validator(
    db: SQLServerConnection,
    trade_date: Optional[date] = None,
    *,
    provider: Optional[MarketDataProvider] = None,
    notifier: Optional[Notifier] = None,
) -> int:
    """Validate every PENDING suggestion whose `entry_date == today`.

    Returns the number of suggestions evaluated (not the number of
    state changes). Tests inject `provider` and `notifier`; production
    falls back to the registry / a default notifier.
    """
    trade_date = trade_date or today_ist()
    p = provider if provider is not None else get_market_data()
    n = notifier if notifier is not None else build_notifier(
        db, provider=getattr(p, "name", None)
    )
    tolerance_pct = float(
        STRATEGY_CONFIG.get("intraday_validator_tolerance_pct", 15.0)
    )

    sug_repo = SuggestionRepo(db)
    rows = db.fetch_all(
        "SELECT * FROM options_suggestions "
        "WHERE status = 'PENDING' AND entry_date = ?",
        [trade_date],
    )
    if not rows:
        logger.info("intraday_validator: no PENDING suggestions for %s", trade_date)
        return 0

    evaluated = 0
    for sug in rows:
        suggestion_id = sug["suggestion_id"]
        legs = sug_repo.legs(suggestion_id)
        if not legs:
            logger.warning(
                "intraday_validator: suggestion %s has no legs — skipping",
                suggestion_id,
            )
            continue

        status, summary = _evaluate_one(
            db, sug, legs,
            trade_date=trade_date, provider=p,
            tolerance_pct=tolerance_pct,
        )
        evaluated += 1

        _set_validator_status(db, suggestion_id, status)

        title_underlying = sug.get("underlying") or "?"
        trade_name = sug.get("trade_name") or suggestion_id

        if status == _STATUS_STILL_GOOD:
            try:
                n.notify(
                    "SUGGESTION_STILL_GOOD",
                    "INFO",
                    f"\u2713 {trade_name} still good at 09:35",
                    summary,
                    related_suggestion_id=suggestion_id,
                )
            except Exception:
                logger.exception(
                    "intraday_validator: notify(STILL_GOOD) failed for %s",
                    suggestion_id,
                )

        elif status == _STATUS_STALE:
            # Flip status so the dashboard hides the now-stale suggestion.
            try:
                sug_repo.update_status(suggestion_id, "IGNORED")
            except Exception:
                logger.exception(
                    "intraday_validator: update_status('IGNORED') failed for %s",
                    suggestion_id,
                )
            try:
                n.notify(
                    "SUGGESTION_STALE",
                    "WARNING",
                    f"\u2717 {trade_name} stale at 09:35 \u2014 do not execute",
                    summary,
                    related_suggestion_id=suggestion_id,
                )
            except Exception:
                logger.exception(
                    "intraday_validator: notify(STALE) failed for %s",
                    suggestion_id,
                )

        else:  # NOT_VALIDATED
            logger.info(
                "intraday_validator: %s NOT_VALIDATED \u2014 %s "
                "(suggestion left PENDING)",
                suggestion_id, summary,
            )

    try:
        db.commit()
    except Exception:
        logger.exception("intraday_validator: commit failed")

    logger.info(
        "intraday_validator: evaluated %d suggestion(s) for %s",
        evaluated, trade_date,
    )
    return evaluated
