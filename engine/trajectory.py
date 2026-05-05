"""
engine/trajectory.py
====================

Pure-function trajectory metrics over a sequence of timestamped numeric
samples (typically 5-min snapshots). Used by the live suggestion engine to
turn a single OI / IV / spread reading into a richer regime descriptor:

    * slope_pct        — % change per sample (linear regression of normalised values)
    * persistence      — fraction of consecutive same-sign deltas (1 = monotonic)
    * acceleration     — second-derivative slope (regime-change detector)
    * noise_floor_check — gate on minimum activity to avoid noisy slopes

All functions return `None` when the input is too short or otherwise
unsuitable (e.g. zero base value for percentage normalisation). Callers
treat `None` as "data not available — leave indicator at None".

No I/O, no clocks, no logging. Fully unit-testable.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _clean(samples: Sequence[Optional[float]]) -> list[float]:
    """Drop None/non-finite entries while preserving order."""
    out: list[float] = []
    for v in samples:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f or f in (float("inf"), float("-inf")):  # NaN / Inf
            continue
        out.append(f)
    return out


def slope_pct(samples: Sequence[Optional[float]]) -> Optional[float]:
    """Linear-regression slope of `samples`, expressed as % of the first
    finite value per sample step.

    Returns None when len(clean) < 3 or the first value is zero.
    """
    xs = _clean(samples)
    if len(xs) < 3:
        return None
    base = xs[0]
    if base == 0.0:
        return None
    n = len(xs)
    # x-axis is sample index 0..n-1
    mean_x = (n - 1) / 2.0
    mean_y = sum(xs) / n
    num = sum((i - mean_x) * (xs[i] - mean_y) for i in range(n))
    den = sum((i - mean_x) ** 2 for i in range(n))
    if den == 0.0:
        return None
    slope_per_step = num / den
    return (slope_per_step / abs(base)) * 100.0


def persistence(
    samples: Sequence[Optional[float]],
    *,
    eps: float = 0.0,
) -> Optional[float]:
    """Fraction of consecutive deltas that share the dominant sign.

    Returns a value in [0.0, 1.0]:
        * 1.0  = strictly monotonic (all deltas same sign)
        * 0.5  = no directional bias (random walk)
        * <0.5 = mean-reverting / oscillating

    Deltas with absolute value <= `eps` are ignored (treated as noise).
    Returns None when fewer than 3 clean samples are available.
    """
    xs = _clean(samples)
    if len(xs) < 3:
        return None
    deltas = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    deltas = [d for d in deltas if abs(d) > eps]
    if not deltas:
        return None
    pos = sum(1 for d in deltas if d > 0)
    neg = sum(1 for d in deltas if d < 0)
    return max(pos, neg) / len(deltas)


def acceleration(samples: Sequence[Optional[float]]) -> Optional[float]:
    """Second-order slope: slope of (slope of consecutive halves).

    Positive acceleration = trend is steepening; negative = trend fading.
    Computes slopes of two adjacent halves and returns the difference.
    Returns None when fewer than 6 clean samples are available.
    """
    xs = _clean(samples)
    if len(xs) < 6:
        return None
    half = len(xs) // 2
    s1 = slope_pct(xs[:half])
    s2 = slope_pct(xs[half:])
    if s1 is None or s2 is None:
        return None
    return s2 - s1


def noise_floor_check(
    activity_samples: Sequence[Optional[float]],
    *,
    min_total: float,
) -> bool:
    """True when the sum of `activity_samples` (e.g. volume per bucket) clears
    `min_total`. Used to gate slope/persistence emission so we don't react to
    micro-movements on near-zero volume.
    """
    xs = _clean(activity_samples)
    return sum(xs) >= float(min_total)
