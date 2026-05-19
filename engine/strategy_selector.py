"""
engine/strategy_selector.py
===========================

The 7-layer decision tree that picks ONE strategy given the market context.

Pure function: takes contracts in, returns a Suggestion (or raises StrategyVeto).

Layers (in order):
    1. IV Rank zone        → WRITING vs BUYING
    2. Trend               → directional vs neutral
    3. PCR confirmation    → biases call vs put side
    4. VIX regime          → veto on SPIKING
    5. Event risk          → veto on this-week HIGH-impact event
    6. DTE band            → veto if outside 7..21
    7. Liquidity / chain   → veto if chain too thin

If any veto fires we raise `StrategyVeto`; the orchestrator converts that to
a `NoSuggestion`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, List, Mapping, Sequence

from config import STRATEGY_CONFIG
from contracts import (
    ConfidenceResult,
    MarketIndicators,
    Suggestion,
    SuggestionEconomics,
    SuggestionLeg,
)
from exceptions import StrategyVeto
from engine import leg_builder
from engine.charges import estimate_charges
from engine.name_generator import make_trade_name
from utils import now_ist


def select_strategy(
    *,
    iv_rank: float,
    trend: str,
    indicators: MarketIndicators,
) -> str:
    """Pick a strategy code from an 11-strategy matrix or raise StrategyVeto.

    Selection axes:
        IV regime  : VERY_HIGH (>butterfly_min) | HIGH (>writing_min) | MID | LOW | VERY_LOW
        Trend      : BULLISH | BEARISH | SIDEWAYS
        Conviction : STRONG | MILD   (from PCR thresholds)
    """
    iv_writing_min   = STRATEGY_CONFIG["iv_rank_writing_min"]
    iv_buying_max    = STRATEGY_CONFIG["iv_rank_buying_max"]
    iv_butterfly_min = STRATEGY_CONFIG.get("iv_rank_butterfly_min",   70.0)
    iv_butterfly_min_prem = STRATEGY_CONFIG.get("iv_butterfly_min_premium", 1.40)
    iv_naked_long_max = STRATEGY_CONFIG.get("iv_rank_naked_long_max", 20.0)
    pcr_bull = STRATEGY_CONFIG.get("pcr_strong_bullish_below", 0.55)
    pcr_bear = STRATEGY_CONFIG.get("pcr_strong_bearish_above", 1.55)
    iv_traj_bias = STRATEGY_CONFIG.get("iv_traj_bias_slope_pct", 0.3)

    pcr = getattr(indicators, "pcr", 1.0)
    strong_bullish = pcr < pcr_bull
    strong_bearish = pcr > pcr_bear

    # Trajectory bias (live-mode only — None in EOD mode).
    # Sustained rising IV near the buying boundary → push into BUYING regime
    # (debit spreads / longs). Sustained falling IV near the writing boundary →
    # push into WRITING regime (credit spreads). Bias only applies when
    # iv_rank is in the ambiguous mid-zone (within 5 points of either boundary)
    # — well-classified rank values are not overridden.
    iv_slope = getattr(indicators, "atm_iv_slope_5min", None)
    iv_persist = getattr(indicators, "atm_iv_persistence", None)
    if iv_slope is not None and iv_persist is not None and iv_persist >= 0.7:
        # Near buying boundary, IV rising sustainedly → treat as buying regime.
        if (
            iv_slope > iv_traj_bias
            and iv_buying_max <= iv_rank < iv_buying_max + 5
        ):
            iv_rank = iv_buying_max - 0.1   # nudge into buying
        # Near writing boundary, IV falling sustainedly → treat as writing regime.
        if (
            iv_slope < -iv_traj_bias
            and iv_writing_min - 5 < iv_rank <= iv_writing_min
        ):
            iv_rank = iv_writing_min + 0.1  # nudge into writing

    # ---------- WRITING regime (high IV) ----------
    if iv_rank > iv_writing_min:
        if trend == "SIDEWAYS":
            # Butterfly only when BOTH (a) IV rank is very high (>70) AND (b) IV is
            # materially above realised vol (iv_premium ≥ 1.40). Otherwise the wide
            # expected move makes ATM short legs too tight → use Iron Condor instead
            # (short legs at EM, more breathing room). iv_premium=None → conservative
            # fallback to Condor (HV-20 may be missing on new underlyings).
            iv_premium = getattr(indicators, "iv_premium", None)
            if iv_rank > iv_butterfly_min and iv_premium is not None \
                    and iv_premium >= iv_butterfly_min_prem:
                return "IRON_BUTTERFLY"
            return "IRON_CONDOR"
        if trend == "BULLISH":
            # Strong bullish conviction at high IV → jade lizard (no upside risk)
            if strong_bullish:
                return "JADE_LIZARD"
            return "BULL_PUT_SPREAD"
        if trend == "BEARISH":
            return "BEAR_CALL_SPREAD"
        raise StrategyVeto(f"Unrecognised trend in writing regime: {trend}")

    # ---------- BUYING regime (low IV) ----------
    if iv_rank < iv_buying_max:
        if trend == "SIDEWAYS":
            return "LONG_STRADDLE"
        if trend == "BULLISH":
            # Very-low IV + strong conviction → naked long call (cheapest, highest leverage)
            if iv_rank < iv_naked_long_max and strong_bullish:
                return "LONG_CALL"
            return "LONG_STRANGLE"
        if trend == "BEARISH":
            if iv_rank < iv_naked_long_max and strong_bearish:
                return "LONG_PUT"
            return "LONG_STRANGLE"
        raise StrategyVeto(f"Unrecognised trend in buying regime: {trend}")

    # ---------- MID-IV zone (30..50) — previously dead-zoned ----------
    # Debit spreads work here: not enough IV to write profitably,
    # but premiums are too rich for naked longs.
    if trend == "BULLISH":
        return "BULL_CALL_SPREAD"
    if trend == "BEARISH":
        return "BEAR_PUT_SPREAD"
    # Sideways in mid-IV remains unactionable — no decent edge for either path
    raise StrategyVeto(
        f"IV Rank {iv_rank:.1f} in mid-zone with sideways trend — no actionable edge"
    )


# ---------------------------------------------------------------------------
# Strategy registry — single source of truth for builder dispatch & metadata.
# ---------------------------------------------------------------------------
def _builder_for(strategy: str):
    """Return a callable(underlying, expiry, chain, spot, expected_move, lots, lot_size) → legs."""
    em_builders = {
        "IRON_CONDOR":      leg_builder.build_iron_condor,
        "BULL_PUT_SPREAD":  leg_builder.build_bull_put_spread,
        "BEAR_CALL_SPREAD": leg_builder.build_bear_call_spread,
        "LONG_STRANGLE":    leg_builder.build_long_strangle,
        "BULL_CALL_SPREAD": leg_builder.build_bull_call_spread,
        "BEAR_PUT_SPREAD":  leg_builder.build_bear_put_spread,
        "IRON_BUTTERFLY":   leg_builder.build_iron_butterfly,
        "JADE_LIZARD":      leg_builder.build_jade_lizard,
    }
    no_em_builders = {
        "LONG_STRADDLE": leg_builder.build_long_straddle,
        "LONG_CALL":     leg_builder.build_long_call,
        "LONG_PUT":      leg_builder.build_long_put,
    }
    return em_builders.get(strategy), no_em_builders.get(strategy)


# Strategies that produce net credit (max_profit = credit, max_loss = width − credit, SL = 1.5× credit)
_CREDIT_STRATEGIES = frozenset({
    "IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD",
    "IRON_BUTTERFLY", "JADE_LIZARD",
})
# Strategies that produce net debit (max_loss = debit, SL = 50% of debit)
_DEBIT_STRATEGIES = frozenset({
    "LONG_STRADDLE", "LONG_STRANGLE", "LONG_CALL", "LONG_PUT",
    "BULL_CALL_SPREAD", "BEAR_PUT_SPREAD",
})


def assemble_suggestion(
    *,
    suggestion_id: str,
    underlying: str,
    expiry: date,
    expiry_type: str,
    dte: int,
    spot: float,
    chain: Sequence[Mapping],
    indicators: MarketIndicators,
    confidence: ConfidenceResult,
    iv_rank: float,
    atm_iv: float,
    lots: int,
    lot_size: int,
    existing_trade_names: Iterable[str] = (),
    generated_on: datetime | None = None,
    strategy_override: str | None = None,
    execution_window: str = "09:20\u201309:45 IST tomorrow",
    data_date: date | None = None,
    entry_date: date | None = None,
    spot_data_date: date | None = None,
    fii_data_date: date | None = None,
    vix_data_date: date | None = None,
    oi_pcr_change: float | None = None,
) -> Suggestion:
    """Top-level: select strategy, build legs, compute economics, return Suggestion.

    If ``strategy_override`` is provided it bypasses ``select_strategy()`` and
    builds legs for that strategy directly.  Used to generate companion
    spread suggestions (BPS / BCS) alongside the primary IC / IB.
    """
    if not confidence.all_passed:
        raise StrategyVeto(
            "Confidence gate not all-pass: " + "; ".join(confidence.failed_reasons)
        )
    if not chain:
        raise StrategyVeto("Empty option chain")

    strategy = strategy_override if strategy_override is not None else select_strategy(
        iv_rank=iv_rank,
        trend=indicators.trend,
        indicators=indicators,
    )

    # Phase 3: strategy-aware soft-gate threshold.
    # Some strategies (naked longs, jade lizard) carry asymmetric risk and warrant
    # a stricter confidence bar than the global 5/7 default.
    strat_overrides = STRATEGY_CONFIG.get("strategy_min_soft_pass", {}) or {}
    required = strat_overrides.get(strategy)
    if required is not None:
        # Soft gates are checks[:7] in confidence.evaluate (gates 1–7).
        soft_checks = list(confidence.checks)[:7]
        soft_pass_count = sum(1 for c in soft_checks if c.status not in ("FAIL", "SOFT_FAIL"))
        if soft_pass_count < required:
            raise StrategyVeto(
                f"{strategy} requires {required}/7 soft gates, got {soft_pass_count}/7"
            )

    # Per-strategy IV/HV ceiling for the BUYING regime.
    # The regime-wide gate in confidence.py is intentionally permissive so that
    # spreads/credit strategies are unaffected by a single shared knob. Strategies
    # listed in `strategy_iv_premium_buy_max` get a stricter veto here, mirroring
    # the `iv_butterfly_min_prem` pattern used for IRON_BUTTERFLY in select_strategy().
    # Strategies NOT in the map are skipped → no behaviour change.
    strat_iv_caps = STRATEGY_CONFIG.get("strategy_iv_premium_buy_max", {}) or {}
    strat_iv_cap = strat_iv_caps.get(strategy)
    if strat_iv_cap is not None:
        iv_prem = getattr(indicators, "iv_premium", None)
        # Only enforce in the buying regime (IV rank low). Writing regime is unaffected.
        iv_buying_max_rank = STRATEGY_CONFIG["iv_rank_buying_max"]
        in_buying_regime = (iv_rank is not None) and (iv_rank < iv_buying_max_rank)
        if in_buying_regime and iv_prem is not None and iv_prem > strat_iv_cap:
            raise StrategyVeto(
                f"{strategy} requires IV/HV \u2264 {strat_iv_cap:.2f}\u00d7 "
                f"(strategy_iv_premium_buy_max), got {iv_prem:.2f}\u00d7 \u2014 "
                f"naked long premium overpays for IV here"
            )

    # Per-strategy IV/HV FLOOR for the WRITING regime (fix D — counterpart of
    # `strategy_iv_premium_buy_max`). Promotes the SOFT_FAIL emitted by
    # confidence._iv_premium_gate to a HARD VETO for the listed strategies
    # when IV/HV < threshold. Catches the live case where IRON_CONDOR was
    # suggested at IV/HV 0.72 (realised vol > implied vol → negative selling
    # edge) and proceeded to lose -₹3,061. Strategies NOT listed skip the
    # check.
    sell_min_overrides = STRATEGY_CONFIG.get("strategy_iv_premium_sell_min", {}) or {}
    sell_min = sell_min_overrides.get(strategy)
    if sell_min is not None:
        iv_prem = getattr(indicators, "iv_premium", None)
        iv_writing_min_rank = STRATEGY_CONFIG["iv_rank_writing_min"]
        in_writing_regime = (iv_rank is not None) and (iv_rank > iv_writing_min_rank)
        if in_writing_regime and iv_prem is not None and iv_prem < float(sell_min):
            raise StrategyVeto(
                f"{strategy} requires IV/HV \u2265 {float(sell_min):.2f}\u00d7 "
                f"(strategy_iv_premium_sell_min), got {iv_prem:.2f}\u00d7 \u2014 "
                f"realised vol exceeds implied: negative selling edge"
            )

    # Per-strategy ADX band (fix D companion). Non-directional credit
    # structures want defined consolidation (mid ADX), not chop (low ADX)
    # and not a trend (high ADX). Both live IRON_CONDOR losers ran on
    # ADX 11-13 (chop with drift) — vetoed by this check at min=15.
    adx_overrides = STRATEGY_CONFIG.get("strategy_adx_band", {}) or {}
    adx_band = adx_overrides.get(strategy)
    if adx_band is not None:
        adx_value = getattr(indicators, "adx_14", None)
        adx_min, adx_max = adx_band
        if adx_value is not None:
            if adx_min is not None and adx_value < float(adx_min):
                raise StrategyVeto(
                    f"{strategy} requires ADX \u2265 {float(adx_min):.0f}, "
                    f"got {adx_value:.1f} \u2014 market is in chop, not "
                    f"consolidation; range-bound credit thesis unreliable"
                )
            if adx_max is not None and adx_value > float(adx_max):
                raise StrategyVeto(
                    f"{strategy} requires ADX \u2264 {float(adx_max):.0f}, "
                    f"got {adx_value:.1f} \u2014 market is trending; "
                    f"directional break risk exceeds range-bound edge"
                )

    # Per-strategy IV/HV buy_pass veto (review item #8 follow-up).
    # `strategy_iv_premium_buy_pass` is the per-strategy "real edge" threshold
    # used by edge_score; promote it to a soft veto when iv_premium exceeds it
    # by more than `iv_premium_buy_pass_tolerance`. Catches marginal-IV buys
    # that previously slipped through because the regime-wide buy_max was much
    # looser than the per-strategy buy_pass. Each strategy carries its own
    # threshold — strategy isolation preserved. Buying regime only.
    buy_pass_overrides = STRATEGY_CONFIG.get("strategy_iv_premium_buy_pass", {}) or {}
    buy_pass = buy_pass_overrides.get(strategy)
    if buy_pass is not None:
        iv_prem = getattr(indicators, "iv_premium", None)
        iv_buying_max_rank = STRATEGY_CONFIG["iv_rank_buying_max"]
        in_buying_regime = (iv_rank is not None) and (iv_rank < iv_buying_max_rank)
        tol = float(STRATEGY_CONFIG.get("iv_premium_buy_pass_tolerance", 0.10))
        threshold = float(buy_pass) * (1.0 + tol)
        if in_buying_regime and iv_prem is not None and iv_prem > threshold:
            raise StrategyVeto(
                f"{strategy} IV/HV {iv_prem:.2f}\u00d7 exceeds buy_pass threshold "
                f"{buy_pass:.2f}\u00d7 (+{tol*100:.0f}% tolerance \u2192 ceiling "
                f"{threshold:.2f}\u00d7) \u2014 marginal buying edge, premium too rich"
            )

    # Build legs by strategy (registry-driven dispatch)
    em_builder, no_em_builder = _builder_for(strategy)
    if em_builder is not None:
        legs = em_builder(
            underlying=underlying, expiry=expiry, chain=chain,
            spot=spot, expected_move=indicators.expected_move,
            lots=lots, lot_size=lot_size,
        )
    elif no_em_builder is not None:
        legs = no_em_builder(
            underlying=underlying, expiry=expiry, chain=chain,
            spot=spot, lots=lots, lot_size=lot_size,
        )
    else:
        raise StrategyVeto(f"Unsupported strategy: {strategy}")

    # Liquidity / pricing veto: any leg with zero suggested price → bail
    if any(l.suggested_price <= 0 for l in legs):
        raise StrategyVeto("Chain too thin — at least one leg has zero/missing price")

    np_per_share = leg_builder.net_premium(legs)

    # Credit-to-width ratio veto: for defined-risk credit strategies the net credit
    # must be at least min_credit_to_width_ratio × spread width.  A condor collecting
    # 3 pts on a 200-pt width (1.5%) doesn't compensate for the risk.
    #
    # Per-strategy override (issue #7): each credit strategy can demand its own
    # minimum via STRATEGY_CONFIG["strategy_min_credit_to_width_ratio"]. Strategies
    # absent from the override map fall back to the regime-wide
    # `min_credit_to_width_ratio`. Strict isolation: tightening one strategy's
    # threshold cannot affect any other strategy.
    cw_default = STRATEGY_CONFIG.get("min_credit_to_width_ratio", 0.20)
    cw_overrides = STRATEGY_CONFIG.get("strategy_min_credit_to_width_ratio", {}) or {}
    min_cw_ratio = float(cw_overrides.get(strategy, cw_default))
    spread_w_for_grade = leg_builder.spread_width(legs)
    if strategy in _CREDIT_STRATEGIES:
        if spread_w_for_grade > 0 and np_per_share < min_cw_ratio * spread_w_for_grade:
            raise StrategyVeto(
                f"Credit-to-width ratio too low: {np_per_share:.1f}/{spread_w_for_grade:.0f} = "
                f"{np_per_share/spread_w_for_grade*100:.1f}% < {min_cw_ratio*100:.0f}% minimum"
            )

    max_profit_ps, max_loss_ps = leg_builder.max_profit_loss(legs, strategy)
    upper_be, lower_be = leg_builder.breakevens(legs, strategy)
    # Pass chain so estimate_pop uses per-strike IV (skew-adjusted) for short legs.
    # Pass strategy so debit / long-premium structures use BE-crossing probability
    # instead of the |Δ_long| approximation (which over-states PoP).
    pop = leg_builder.estimate_pop(legs, spot, dte, atm_iv, chain=chain, strategy=strategy)

    # Charges (per-share priced — we want totals → multiply by qty)
    charges = estimate_charges([
        {
            "action":   l.action,
            "price":    l.suggested_price,
            "lots":     l.lots,
            "lot_size": l.lot_size,
        }
        for l in legs
    ])

    qty_total = sum(l.lots * l.lot_size for l in legs)
    # np_per_share already computed above; store that as the per-unit net credit
    # (lots × lot_size scaling belongs only in total-credit display, not this field)
    # Max profit/loss in rupees (defined-risk strategies)
    if max_profit_ps == float("inf"):
        max_profit_rs = float("inf")
    else:
        # For credit strategies max profit is net credit per single contract
        max_profit_rs = max_profit_ps * (legs[0].lot_size if legs else 1) * (legs[0].lots if legs else 1)
    max_loss_rs = max_loss_ps * (legs[0].lot_size if legs else 1) * (legs[0].lots if legs else 1)

    estimated_net_pnl = (max_profit_rs if max_profit_rs != float("inf") else 0.0) - charges.total

    economics = SuggestionEconomics(
        net_credit=round(np_per_share, 2),  # per-unit (per-share) net credit/debit
        max_profit=round(max_profit_rs, 2) if max_profit_rs != float("inf") else float("inf"),
        max_loss=round(max_loss_rs, 2),
        upper_breakeven=upper_be,
        lower_breakeven=lower_be,
        stop_loss_level=_compute_stop_loss(legs, strategy, max_loss_rs),
        probability_of_profit=round(pop, 1),
        estimated_charges=charges,
        estimated_net_pnl=round(estimated_net_pnl, 2),
    )

    # Numeric edge score (issue #10) — display + ranking only, never gates.
    # Per-strategy weighted blend of PoP, credit-to-width grade (or debit
    # discount), IV regime alignment, and confidence headroom.
    from engine import edge_score as _edge
    _grade = (
        _edge.grade_credit_to_width(np_per_share / spread_w_for_grade)
        if (strategy in _CREDIT_STRATEGIES and spread_w_for_grade > 0)
        else None
    )
    _score, _components = _edge.compute(
        strategy=strategy,
        pop=pop,
        legs=legs,
        net_premium_per_share=np_per_share,
        spread_width=spread_w_for_grade,
        iv_rank=iv_rank,
        iv_premium=getattr(indicators, "iv_premium", None),
        confidence=confidence,
    )
    economics.edge_score = _score
    economics.edge_score_components = _components
    economics.credit_grade = _grade

    trade_name = make_trade_name(
        underlying=underlying,
        strategy=strategy,
        expiry=expiry,
        existing_names=existing_trade_names,
    )

    plain_english = _explain(strategy, underlying, indicators, iv_rank, dte, economics,
                             execution_window)

    return Suggestion(
        suggestion_id=suggestion_id,
        trade_name=trade_name,
        generated_on=generated_on or now_ist(),
        strategy=strategy,
        strategy_type=("WRITING" if strategy in _CREDIT_STRATEGIES else "BUYING"),
        underlying=underlying,
        expiry_date=expiry,
        expiry_type=expiry_type,
        dte=dte,
        spot_at_generation=spot,
        confidence=confidence,
        legs=legs,
        economics=economics,
        execution_window=execution_window,
        plain_english=plain_english,
        data_date=data_date,
        entry_date=entry_date,
        spot_data_date=spot_data_date,
        fii_data_date=fii_data_date,
        vix_data_date=vix_data_date,
        oi_pcr_change=oi_pcr_change,
    )


def _compute_stop_loss(
    legs: Sequence[SuggestionLeg], strategy: str, max_loss_rs: float
) -> float | None:
    """Return a spot-level SL based on short strikes + 50% of wing width.

    For a call spread (or two-sided strategy), SL = short_call + 50% of CE wing.
    For a put-only spread, SL = short_put - 50% of PE wing.
    The frontend derives the complementary put/call SL symmetrically for two-sided
    strategies (Iron Condor, Iron Butterfly) using the stored call-side SL.
    Debit strategies have no meaningful single spot-level SL; return None.
    """
    sc = next((l for l in legs if l.action == "SELL" and l.option_type == "CE"), None)
    sp = next((l for l in legs if l.action == "SELL" and l.option_type == "PE"), None)
    lc = next((l for l in legs if l.action == "BUY"  and l.option_type == "CE"), None)
    lp = next((l for l in legs if l.action == "BUY"  and l.option_type == "PE"), None)

    # Strategies with a call spread: SL = short call + 50% of CE wing width
    if sc and lc:
        ce_width = lc.strike - sc.strike
        return round(sc.strike + ce_width * 0.5)
    # Put-only spreads (no short call): SL = short put - 50% of PE wing width
    if sp and lp and not sc:
        pe_width = sp.strike - lp.strike
        return round(sp.strike - pe_width * 0.5)
    # Debit strategies / naked longs: no spot-level SL
    return None


def _explain(
    strategy: str,
    underlying: str,
    indicators: MarketIndicators,
    iv_rank: float,
    dte: int,
    econ: SuggestionEconomics,
    execution_window: str = "09:20\u201309:45 IST tomorrow",
) -> str:
    parts: List[str] = []
    parts.append(f"{strategy.replace('_', ' ').title()} on {underlying}.")
    parts.append(f"IV Rank {iv_rank:.0f}, trend {indicators.trend.lower()}, "
                 f"VIX {indicators.vix_close:.1f} ({indicators.vix_regime.lower()}).")
    parts.append(f"Entry DTE {dte}, expected move \u00b1{indicators.expected_move:.0f} pts.")
    if econ.upper_breakeven is not None and econ.lower_breakeven is not None:
        parts.append(f"Profit zone: {econ.lower_breakeven:.0f}\u2013{econ.upper_breakeven:.0f}.")
    elif econ.upper_breakeven is not None:
        parts.append(f"Breakeven up to {econ.upper_breakeven:.0f}.")
    elif econ.lower_breakeven is not None:
        parts.append(f"Profit if {underlying} stays above {econ.lower_breakeven:.0f}.")
    parts.append(f"PoP \u2248 {econ.probability_of_profit:.0f}%.")
    intro = " ".join(parts)

    lines: List[str] = [intro, ""]

    # Entry
    lines.append("ENTRY THRESHOLDS")
    lines.append(f"\u2022 {execution_window}")
    lines.append("")

    # Timeline (SL/target omitted \u2014 the frontend computes those from leg data)
    lines.append("TIMELINE")
    if strategy in _CREDIT_STRATEGIES:
        d1 = round(dte * 0.35)
        d2 = round(dte * 0.55)
        close_before = max(2, round(dte * 0.25))   # ~2 days for weekly, ~5 for monthly
        lines.append(f"\u2022 Monitor theta decay daily; peak decay around day {d1}\u2013{d2}")
        lines.append(f"\u2022 Theta accelerates in the final {close_before} DTE \u2014 close or roll before expiry")
    elif strategy in _DEBIT_STRATEGIES:
        half = max(1, dte // 2)
        close_before = max(2, round(dte * 0.25))
        lines.append(f"\u2022 Look for a decisive move within {half} days; time decay works against you")
        lines.append(f"\u2022 Close before the final {close_before} DTE if the move has not materialised")

    return "\n".join(lines)
