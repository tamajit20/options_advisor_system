"""
engine/em_calibration.py
========================

Expected-move calibration validator (review item #10).

Background
----------
The strategy engine sizes credit-spread short strikes off
``expected_move = spot × atm_iv × √(DTE/365)``.  This is the lognormal
1σ envelope assumed by Black-Scholes.  In practice realised |close-close|
moves over comparable DTE windows can systematically over- or undershoot
that envelope (vol smile skew, persistent IV crush, BANKNIFTY's fat
tails post-derivatives transition).  When that happens, condor short
strikes are systematically too narrow (or too wide) and PoP is
mis-estimated.

This module is pure logic — no DB, no I/O.  It exposes:

* :func:`compute_realised_ratio` — turn a settled suggestion into a
  ``realised / expected`` ratio.
* :func:`band_dte` — bucket a DTE into ``"0-7" / "8-21" / "22+"`` so
  comparable expiries calibrate together.
* :func:`compute_calibration_warning` — given the historical sample of
  ``realised_ratio`` rows for a single (underlying, dte_band), decide
  whether to surface a warning chip on a new suggestion.

Persistence and the settle-time hook live in
``lifecycle/em_calibration_recorder.py``.  The dashboard rendering lives
in the suggestion serializer / dashboard JS.
"""

from __future__ import annotations

import math
from statistics import median
from typing import List, Optional, Sequence


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def compute_realised_ratio(
    spot_at_entry: float,
    spot_at_expiry: float,
    expected_move: float,
) -> Optional[float]:
    """Return ``|spot_at_expiry - spot_at_entry| / expected_move``.

    Returns ``None`` when ``expected_move`` is non-positive — guards against
    division by zero for malformed rows (zero IV, zero DTE).
    """
    if expected_move is None or expected_move <= 0:
        return None
    return abs(float(spot_at_expiry) - float(spot_at_entry)) / float(expected_move)


def band_dte(dte: int) -> str:
    """Bucket a DTE into one of three calibration bands.

    Bands are coarse on purpose — finer bins would require more historical
    samples than a low-frequency advisor accumulates.
    """
    if dte is None:
        return "unknown"
    d = int(dte)
    if d <= 7:
        return "0-7"
    if d <= 21:
        return "8-21"
    return "22+"


# ---------------------------------------------------------------------------
# Warning emission
# ---------------------------------------------------------------------------
def compute_calibration_warning(
    samples: Sequence[float],
    *,
    underlying: str,
    dte: int,
    min_samples: int,
    deviation_threshold: float,
) -> Optional[str]:
    """Return a warning string when historical realised/expected deviates
    materially from 1.0 for the (underlying, dte_band) cohort, else ``None``.

    Parameters
    ----------
    samples
        Realised/expected ratios from the most recent settled expiries that
        match ``(underlying, band_dte(dte))``.  Most-recent first or last —
        we only look at the median so order is irrelevant.
    underlying, dte
        Used purely for the user-facing message.
    min_samples
        Below this we suppress any warning — the median is statistically
        meaningless on tiny cohorts.  Typical: ``4``.
    deviation_threshold
        Absolute distance from 1.0 at which a warning fires.  ``0.25`` ⇒
        warn when the median realised/expected is < 0.75 or > 1.25.

    Notes
    -----
    * Median is preferred over mean: a single news-driven blow-out (e.g. a
      surprise rate cut) shouldn't permanently bias the chip.
    * The returned string is plain text — the dashboard layer is
      responsible for wrapping it in a chip element.
    """
    valid = [float(r) for r in samples if r is not None and math.isfinite(r) and r >= 0.0]
    if len(valid) < int(min_samples):
        return None
    med = median(valid)
    if not math.isfinite(med) or med <= 0:
        return None
    deviation = abs(med - 1.0)
    if deviation < float(deviation_threshold):
        return None
    direction = "over" if med > 1.0 else "under"
    band = band_dte(dte)
    return (
        f"EM {direction}-calibrated for {underlying} @ {band} DTE: "
        f"realised {med:.2f}× over last {len(valid)} expiries"
    )


__all__ = [
    "compute_realised_ratio",
    "band_dte",
    "compute_calibration_warning",
]
