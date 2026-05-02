"""
lifecycle/suggestion_engine.py
==============================

Daily suggestion-generation orchestrator.

For each underlying:
    1. Pick the nearest expiry within DTE band (7..21)
    2. Build market indicators (PCR, max pain, ATR, trend, VIX regime, EM)
    3. Run confidence gate
    4. If 7/7 â†’ assemble suggestion via strategy_selector
       Else / on StrategyVeto â†’ record NoSuggestion
    5. Persist (Suggestion + legs OR NoSuggestion)
    6. Emit notification

Generates AT MOST one suggestion per underlying per day. We pick the
highest-confidence underlying as "the" suggestion of the day.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

from config import STRATEGY_CONFIG
from contracts import (
    ConfidenceResult,
    MarketIndicators,
    NoSuggestion,
    Notification,
    Suggestion,
)
from database.connection import SQLServerConnection
from database.models import (
    EventCalendarRepo,
    FoEodRepo,
    IvHistoryRepo,
    LotSizeRepo,
    NotificationRepo,
    SpotEodRepo,
    SuggestionRepo,
    TradeRepo,
    VixRepo,
)
from engine.confidence import evaluate as evaluate_confidence
from engine.indicators import build_indicators
from engine.strategy_selector import assemble_suggestion
from exceptions import StrategyVeto
from utils import days_between, now_ist, today_ist

logger = logging.getLogger(__name__)


def _pick_expiry_in_band(
    fo: FoEodRepo, symbol: str, trade_date: date
) -> Optional[date]:
    expiries = fo.expiries_for(symbol, trade_date)
    dte_min = STRATEGY_CONFIG["dte_min"]
    dte_max = STRATEGY_CONFIG["dte_max"]
    in_band = [e for e in expiries
               if dte_min <= days_between(trade_date, e) <= dte_max]
    if not in_band:
        return None
    return min(in_band)  # nearest in band


def _evaluate_underlying(
    db: SQLServerConnection,
    symbol: str,
    trade_date: date,
) -> tuple[Optional[Suggestion], Optional[NoSuggestion]]:
    """Returns (suggestion, no_suggestion). Exactly one is non-None."""
    fo = FoEodRepo(db)
    sp = SpotEodRepo(db)
    vix_repo = VixRepo(db)
    iv_repo = IvHistoryRepo(db)
    lot_repo = LotSizeRepo(db)
    event_repo = EventCalendarRepo(db)

    spot_row = sp.latest(symbol)
    if not spot_row:
        logger.warning("Suggestion: no spot for %s", symbol)
        return None, None
    spot = float(spot_row["close_price"])

    expiry = _pick_expiry_in_band(fo, symbol, trade_date)
    if expiry is None:
        return None, None
    dte = days_between(trade_date, expiry)

    chain = fo.get_chain(symbol, trade_date, expiry)
    if not chain:
        return None, None

    iv_rows = iv_repo.latest_for(symbol, trade_date)
    iv_for_expiry = [r for r in iv_rows if r.get("expiry_date") == expiry]
    if not iv_for_expiry:
        logger.warning("Suggestion: no IV rows for %s exp=%s", symbol, expiry)
        return None, None
    atm_iv = float(iv_for_expiry[0].get("atm_iv") or 0.0)
    iv_rank = float(iv_for_expiry[0].get("iv_rank") or 0.0)

    # Market indicators
    spot_history_since = trade_date - timedelta(days=120)
    spot_history = sp.history(symbol, spot_history_since)
    vix_history = vix_repo.history(trade_date - timedelta(days=10))

    indicators = build_indicators(
        symbol=symbol,
        as_of=trade_date,
        spot=spot,
        chain_rows=chain,
        spot_history=spot_history,
        vix_history=vix_history,
        atm_iv=atm_iv,
        dte=dte,
    )

    # Events this week
    week_end = trade_date + timedelta(days=7)
    has_event = event_repo.has_high_impact(trade_date, week_end)

    confidence = evaluate_confidence(
        iv_rank=iv_rank,
        indicators=indicators,
        dte=dte,
        has_high_impact_event_this_week=has_event,
    )

    if not confidence.all_passed:
        ns = NoSuggestion(
            generated_on=now_ist(),
            underlying=symbol,
            confidence=confidence,
            reason=f"Confidence {confidence.score}/{confidence.total}: "
                   + "; ".join(confidence.failed_reasons),
        )
        return None, ns

    # Assemble â€” may raise StrategyVeto
    lot_size = (lot_repo.for_symbol(symbol, trade_date)
                or STRATEGY_CONFIG["default_lot_sizes"].get(symbol, 50))
    sug_repo = SuggestionRepo(db)
    suggestion_id = sug_repo.next_suggestion_id(trade_date)

    # Existing trade names â€” for collision avoidance
    trade_repo = TradeRepo(db)
    existing_names = [t.get("trade_name") for t in trade_repo.open_trades()
                      if t.get("trade_name")]

    try:
        suggestion = assemble_suggestion(
            suggestion_id=suggestion_id,
            underlying=symbol,
            expiry=expiry,
            dte=dte,
            spot=spot,
            chain=chain,
            indicators=indicators,
            confidence=confidence,
            iv_rank=iv_rank,
            atm_iv=atm_iv,
            lots=1,
            lot_size=lot_size,
            existing_trade_names=existing_names,
            generated_on=now_ist(),
        )
        return suggestion, None
    except StrategyVeto as veto:
        ns = NoSuggestion(
            generated_on=now_ist(),
            underlying=symbol,
            confidence=confidence,
            reason=f"Strategy veto: {veto}",
        )
        return None, ns


def run_suggestion_engine(
    db: SQLServerConnection,
    trade_date: date | None = None,
) -> int:
    """Evaluate all configured underlyings and persist every one that passes
    all confidence checks (one suggestion per underlying per day). Underlyings
    that already have a suggestion for today are skipped so re-runs are safe.

    Returns number of new suggestions persisted.
    """
    trade_date = trade_date or today_ist()
    sug_repo = SuggestionRepo(db)
    notif_repo = NotificationRepo(db)

    persisted: List[Suggestion] = []
    no_suggestions: List[NoSuggestion] = []

    for symbol in STRATEGY_CONFIG["underlyings"]:
        # Dedup guard -- skip if already suggested this underlying today
        if sug_repo.has_suggestion_for(symbol, trade_date):
            logger.info("Suggestion already exists for %s on %s -- skipping", symbol, trade_date)
            continue
        try:
            sug, ns = _evaluate_underlying(db, symbol, trade_date)
        except Exception:
            logger.exception("Suggestion eval failed for %s", symbol)
            continue
        if sug is not None:
            sug_repo.insert(sug)
            sug_repo.insert_legs(sug.suggestion_id, sug.legs)
            notif_repo.insert(Notification(
                created_at=now_ist(),
                notif_type="NEW_SUGGESTION",
                severity="INFO",
                title=f"New suggestion: {sug.trade_name}",
                body=sug.plain_english,
                related_suggestion_id=sug.suggestion_id,
            ))
            persisted.append(sug)
            logger.info("Persisted suggestion %s (%s %s)", sug.suggestion_id, sug.underlying, sug.strategy)
        elif ns is not None:
            no_suggestions.append(ns)

    import json as _json
    for ns in no_suggestions:
        try:
            ns_id = sug_repo.next_suggestion_id(trade_date)
            conditions = {
                "score":          ns.confidence.score,
                "total":          ns.confidence.total,
                "passed":         ns.confidence.conditions_met,
                "failed":         ns.confidence.conditions_failed,
                "failed_reasons": ns.confidence.failed_reasons,
            }
            sug_repo.insert_no_suggestion(
                suggestion_id=ns_id,
                underlying=ns.underlying,
                generated_on=ns.generated_on,
                confidence_score=ns.confidence.score,
                conditions_json=_json.dumps(conditions),
                reason=ns.reason,
            )
        except Exception:
            logger.exception("Failed to persist NoSuggestion for %s", ns.underlying)

    if not persisted and no_suggestions:
        notif_repo.insert(Notification(
            created_at=now_ist(),
            notif_type="NO_SUGGESTION",
            severity="INFO",
            title="No suggestion today",
            body="; ".join(f"{n.underlying}: {n.reason}" for n in no_suggestions),
        ))

    db.commit()
    logger.info(
        "Suggestion engine done: %d suggested, %d no-suggestion",
        len(persisted), len(no_suggestions),
    )
    return len(persisted)
