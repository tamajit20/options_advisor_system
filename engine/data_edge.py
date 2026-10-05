"""
Advisory notes from data the engine already has.

These checks are kind=ADVISORY. Callers must merge them without changing
``all_passed``, so a weak note never hides the suggestion.
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from config import STRATEGY_CONFIG
from contracts import ConfidenceCheck, MarketIndicators, SuggestionLeg

_RANGE = frozenset({"IRON_CONDOR", "IRON_BUTTERFLY", "CALENDAR_SPREAD"})
_BULLISH = frozenset({
    "BULL_PUT_SPREAD", "JADE_LIZARD", "BULL_CALL_SPREAD", "LONG_CALL",
})
_BEARISH = frozenset({
    "BEAR_CALL_SPREAD", "BEAR_PUT_SPREAD", "LONG_PUT",
})
_LONG_VOL = frozenset({"LONG_STRADDLE", "LONG_STRANGLE"})
_CREDIT = frozenset({
    "IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD",
    "IRON_BUTTERFLY", "JADE_LIZARD",
})


def _note(label: str, status: str, detail: str) -> ConfidenceCheck:
    return ConfidenceCheck(label=label, status=status, detail=detail, kind="ADVISORY")


def build_data_edge_checks(
    *,
    strategy: str,
    iv_rank: Optional[float],
    indicators: MarketIndicators,
    legs: Sequence[SuggestionLeg],
) -> List[ConfidenceCheck]:
    """Visible notes only. Missing data is omitted or PASS_WARN — never a veto."""
    out: List[ConfidenceCheck] = []
    mid = _mid_iv_note(iv_rank)
    if mid is not None:
        out.append(mid)
    oi = _oi_pcr_range_note(strategy, indicators)
    if oi is not None:
        out.append(oi)
    fii = _fii_options_note(strategy, indicators)
    if fii is not None:
        out.append(fii)
    pain = _max_pain_note(strategy, indicators, legs)
    if pain is not None:
        out.append(pain)
    burst = _volume_burst_note(strategy, indicators)
    if burst is not None:
        out.append(burst)
    return out


def _mid_iv_note(iv_rank: Optional[float]) -> Optional[ConfidenceCheck]:
    if iv_rank is None:
        return None
    lo = float(STRATEGY_CONFIG.get("iv_rank_buying_max", 30))
    hi = float(STRATEGY_CONFIG.get("iv_rank_writing_min", 50))
    rank = float(iv_rank)
    if not (lo <= rank <= hi):
        return None
    return _note(
        "Mid-IV zone",
        "SOFT_FAIL",
        f"IV Rank {rank:.0f} is in the {lo:.0f}–{hi:.0f} band — "
        f"vol edge is thinner here; suggestion still shown",
    )


def _oi_pcr_range_note(strategy: str, indicators: MarketIndicators) -> Optional[ConfidenceCheck]:
    if strategy not in _RANGE:
        return None
    slope = indicators.oi_pcr_slope_5min
    persist = indicators.oi_pcr_persistence
    if slope is None or persist is None:
        return None
    slope_warn = float(STRATEGY_CONFIG.get("oi_pcr_traj_slope_warn_pct", 1.0))
    persist_warn = float(STRATEGY_CONFIG.get("oi_pcr_traj_persistence_warn", 0.7))
    if abs(float(slope)) > slope_warn and float(persist) >= persist_warn:
        return _note(
            "Range trade vs OI PCR drift",
            "SOFT_FAIL",
            f"OI PCR slope {float(slope):+.2f}%/5min sustained {float(persist)*100:.0f}% — "
            f"flow is directional; range suggestion still shown",
        )
    return None


def _fii_options_note(strategy: str, indicators: MarketIndicators) -> Optional[ConfidenceCheck]:
    calls = indicators.fii_net_calls
    puts = indicators.fii_net_puts
    if calls is None or puts is None:
        return None
    thresh = float(STRATEGY_CONFIG.get("fii_options_skew_threshold", 100_000))
    # Positive = FII longer calls than puts (bullish options book).
    skew = float(calls) - float(puts)
    if strategy in _BULLISH and skew <= -thresh:
        return _note(
            "FII options book",
            "SOFT_FAIL",
            f"FII options skew {skew:,.0f} contracts (puts heavier than calls) "
            f"vs this bullish pick — suggestion still shown",
        )
    if strategy in _BEARISH and skew >= thresh:
        return _note(
            "FII options book",
            "SOFT_FAIL",
            f"FII options skew {skew:,.0f} contracts (calls heavier than puts) "
            f"vs this bearish pick — suggestion still shown",
        )
    return None


def _max_pain_note(
    strategy: str,
    indicators: MarketIndicators,
    legs: Sequence[SuggestionLeg],
) -> Optional[ConfidenceCheck]:
    if strategy not in _CREDIT:
        return None
    pain = float(indicators.max_pain or 0)
    if pain <= 0:
        return None
    em = float(indicators.expected_move or 0)
    buffer = max(50.0, em * 0.15)
    beyond: List[str] = []
    for leg in legs:
        if str(leg.action).upper() != "SELL":
            continue
        strike = float(leg.strike)
        opt = str(leg.option_type).upper()
        if opt == "CE" and pain > strike + buffer:
            beyond.append(f"short {strike:.0f} CE (max pain {pain:.0f} is above it)")
        elif opt == "PE" and pain < strike - buffer:
            beyond.append(f"short {strike:.0f} PE (max pain {pain:.0f} is below it)")
    if not beyond:
        return None
    return _note(
        "Max pain vs short strikes",
        "SOFT_FAIL",
        "Pin risk: " + "; ".join(beyond) + " — credit suggestion still shown",
    )


def _volume_burst_note(strategy: str, indicators: MarketIndicators) -> Optional[ConfidenceCheck]:
    if strategy not in _LONG_VOL:
        return None
    z = indicators.volume_burst_z
    if z is None:
        return None
    quiet = float(STRATEGY_CONFIG.get("volume_burst_quiet_z", 0.5))
    if float(z) >= quiet:
        return None
    return _note(
        "Volume burst",
        "SOFT_FAIL",
        f"Volume burst z={float(z):.2f} is below {quiet:.2f} — "
        f"quiet tape; long-vol suggestion still shown",
    )
