"""
lifecycle/em_calibration_recorder.py
=====================================

Settle-time hook for review item #10 — expected-move calibration validator.

Run after fo_bhav lands the **expiry close**.  For every suggestion whose
``expiry_date == settled_date`` that does not yet have a calibration row
we recover the inputs that were active when the suggestion was generated:

* ``spot_at_entry``        ← ``options_suggestions.spot_at_generation``
* ``dte_at_entry``         ← ``options_suggestions.dte``
* ``atm_iv_at_entry``      ← AVG ``atm_iv`` from ``options_iv_history`` at
                             ``(underlying, data_date)`` (whichever IV
                             snapshot the engine actually consumed).
* ``spot_at_expiry``       ← ``options_spot_eod.close_price`` at
                             ``(underlying, expiry_date)``.

We then compute::

    expected_move  = spot_at_entry × atm_iv × √(dte / 365)
    realised_move  = |spot_at_expiry − spot_at_entry|
    realised_ratio = realised_move / expected_move

…and persist a row to ``options_em_calibration``.  The recorder is
idempotent — already-calibrated suggestions are skipped.

Why a separate recorder
-----------------------
We do not piggy-back on ``exit_orchestrator`` because trades may have
been closed early (stop-loss, exit signals) and never reach
expiry.  The calibration is a property of the suggestion's intent, not
of the actual trade outcome.  Every PENDING / EXECUTED suggestion
contributes one calibration sample at expiry.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Optional

from database.connection import SQLServerConnection
from database.models import (
    EmCalibrationRepo,
    IvHistoryRepo,
    SpotEodRepo,
)
from engine.em_calibration import band_dte, compute_realised_ratio

logger = logging.getLogger(__name__)


def _atm_iv_for(db: SQLServerConnection, symbol: str, on_date: date) -> Optional[float]:
    """Return the average ATM IV at ``(symbol, on_date)`` from
    ``options_iv_history``.

    Returns ``None`` when the row is missing or the stored value is
    ≤ 0 (the IV calculator writes 0 when convergence failed — that
    snapshot can't anchor a meaningful expected move).
    """
    rows = IvHistoryRepo(db).latest_for(symbol, on_date)
    vals = [float(r["atm_iv"]) for r in rows
            if r.get("atm_iv") is not None and float(r["atm_iv"]) > 0]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _spot_close_on(db: SQLServerConnection, symbol: str, on_date: date) -> Optional[float]:
    row = SpotEodRepo(db).for_date(symbol, on_date)
    if not row or row.get("close_price") is None:
        return None
    try:
        return float(row["close_price"])
    except (TypeError, ValueError):
        return None


def record_settled_expiries(db: SQLServerConnection, settled_date: date) -> int:
    """Persist EM-calibration rows for every suggestion that expired on
    ``settled_date`` and isn't yet calibrated.

    Returns the number of rows inserted.  Suggestions for which the lookup
    fails (missing IV snapshot, missing settlement spot) are silently
    skipped — they cannot produce a meaningful sample.
    """
    repo = EmCalibrationRepo(db)
    candidates = repo.settled_suggestions_pending_calibration(settled_date)
    if not candidates:
        return 0
    inserted = 0
    for s in candidates:
        try:
            sid = s["suggestion_id"]
            underlying = s["underlying"]
            dte = int(s["dte"])
            spot_at_entry = float(s["spot_at_generation"])
            data_date = s.get("data_date") or s.get("generated_on")
            # generated_on may be a datetime — coerce to date
            if hasattr(data_date, "date"):
                data_date = data_date.date()
            atm_iv = _atm_iv_for(db, underlying, data_date)
            if atm_iv is None or atm_iv <= 0:
                logger.debug(
                    "EM-calib: no usable atm_iv for %s on %s (suggestion %s) — skipping",
                    underlying, data_date, sid,
                )
                continue
            if dte <= 0 or spot_at_entry <= 0:
                logger.debug(
                    "EM-calib: degenerate inputs for %s (dte=%s, spot=%s) — skipping",
                    sid, dte, spot_at_entry,
                )
                continue
            expected_move = spot_at_entry * atm_iv * math.sqrt(dte / 365.0)
            spot_at_expiry = _spot_close_on(db, underlying, settled_date)
            if spot_at_expiry is None:
                logger.debug(
                    "EM-calib: no spot close for %s on %s — skipping suggestion %s",
                    underlying, settled_date, sid,
                )
                continue
            ratio = compute_realised_ratio(spot_at_entry, spot_at_expiry, expected_move)
            if ratio is None:
                continue
            realised_move = abs(spot_at_expiry - spot_at_entry)
            repo.insert_one({
                "suggestion_id":   sid,
                "underlying":      underlying,
                "generated_on":    data_date,
                "expiry_date":     settled_date,
                "dte_at_entry":    dte,
                "dte_band":        band_dte(dte),
                "spot_at_entry":   spot_at_entry,
                "spot_at_expiry":  spot_at_expiry,
                "atm_iv_at_entry": atm_iv,
                "expected_move":   expected_move,
                "realised_move":   realised_move,
                "realised_ratio":  ratio,
            })
            inserted += 1
        except Exception:
            logger.exception(
                "EM-calib: failed to record suggestion %s — skipping",
                s.get("suggestion_id"),
            )
    if inserted:
        logger.info(
            "EM-calib: recorded %d realised/expected sample(s) for expiry %s",
            inserted, settled_date,
        )
    return inserted


__all__ = ["record_settled_expiries"]
