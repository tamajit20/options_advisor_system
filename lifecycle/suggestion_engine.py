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


def _is_monthly_expiry(expiry: date) -> bool:
    """True when expiry is the last Thursday of its calendar month (NSE monthly F&O)."""
    if expiry.weekday() != 3:   # must be Thursday
        return False
    return (expiry + timedelta(days=7)).month != expiry.month


def _pick_expiries_in_band(
    fo: FoEodRepo, symbol: str, trade_date: date
) -> list[tuple[date, str]]:
    """Return [(expiry, expiry_type), ...] for the DTE band — at most one Weekly and one Monthly.

    Rules:
    - Collect all expiries within [dte_min, dte_max].
    - If monthly and weekly fall on the same Thursday (last week of month),
      return only that date tagged as 'Monthly'.
    - Otherwise return nearest monthly + nearest weekly (both, if different dates).
    """
    expiries = fo.expiries_for(symbol, trade_date)
    dte_min = STRATEGY_CONFIG["dte_min"]
    dte_max = STRATEGY_CONFIG["dte_max"]
    in_band = [e for e in expiries
               if dte_min <= days_between(trade_date, e) <= dte_max]
    if not in_band:
        return []

    monthly = sorted(e for e in in_band if _is_monthly_expiry(e))
    weekly  = sorted(e for e in in_band if not _is_monthly_expiry(e))

    result: list[tuple[date, str]] = []
    chosen_monthly = monthly[0] if monthly else None
    chosen_weekly  = weekly[0]  if weekly  else None

    if chosen_monthly:
        result.append((chosen_monthly, "Monthly"))
    if chosen_weekly and chosen_weekly != chosen_monthly:
        result.append((chosen_weekly, "Weekly"))

    return result


def _evaluate_underlying(
    db: SQLServerConnection,
    symbol: str,
    trade_date: date,
) -> tuple[list[Suggestion], list[NoSuggestion]]:
    """Evaluate one underlying for all expiry types in the DTE band.

    Returns (suggestions, no_suggestions) — may have up to 2 suggestions
    (one Monthly + one Weekly) or multiple NoSuggestion records.
    """
    fo = FoEodRepo(db)
    sp = SpotEodRepo(db)
    vix_repo = VixRepo(db)
    iv_repo = IvHistoryRepo(db)
    lot_repo = LotSizeRepo(db)
    event_repo = EventCalendarRepo(db)

    spot_row = sp.latest(symbol)
    if not spot_row:
        logger.warning("Suggestion: no spot for %s", symbol)
        return [], []
    spot = float(spot_row["close_price"])

    expiry_candidates = _pick_expiries_in_band(fo, symbol, trade_date)
    if not expiry_candidates:
        return [], []

    # Shared data fetched once for all expiry candidates
    spot_history_since = trade_date - timedelta(days=120)
    spot_history = sp.history(symbol, spot_history_since)
    vix_history = vix_repo.history(trade_date - timedelta(days=10))
    week_end = trade_date + timedelta(days=7)
    has_event = event_repo.has_high_impact(trade_date, week_end)
    event_row = event_repo.first_high_impact_event(trade_date, week_end)
    events_total = event_repo.count_all()
    event_desc = ""
    if event_row:
        ed = event_row.get("event_date", "")
        et = event_row.get("description") or event_row.get("event_type", "")
        event_desc = f"{et} on {ed}" if ed else et

    lot_size = (lot_repo.for_symbol(symbol, trade_date)
                or STRATEGY_CONFIG["default_lot_sizes"].get(symbol, 50))
    sug_repo = SuggestionRepo(db)
    trade_repo = TradeRepo(db)
    existing_names = [t.get("trade_name") for t in trade_repo.open_trades()
                      if t.get("trade_name")]

    iv_rows = iv_repo.latest_for(symbol, trade_date)

    suggestions: list[Suggestion] = []
    no_suggestions: list[NoSuggestion] = []

    for expiry, expiry_type in expiry_candidates:
        dte = days_between(trade_date, expiry)

        chain = fo.get_chain(symbol, trade_date, expiry)
        if not chain:
            continue

        iv_for_expiry = [r for r in iv_rows if r.get("expiry_date") == expiry]
        if not iv_for_expiry:
            logger.warning("Suggestion: no IV rows for %s exp=%s (%s)", symbol, expiry, expiry_type)
            continue
        atm_iv = float(iv_for_expiry[0].get("atm_iv") or 0.0)
        _raw_iv_rank = iv_for_expiry[0].get("iv_rank")
        iv_rank: Optional[float] = float(_raw_iv_rank) if _raw_iv_rank is not None else None

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

        confidence = evaluate_confidence(
            iv_rank=iv_rank,
            indicators=indicators,
            dte=dte,
            has_high_impact_event_this_week=has_event,
            high_impact_event_description=event_desc,
            events_calendar_row_count=events_total,
        )

        if not confidence.all_passed:
            no_suggestions.append(NoSuggestion(
                generated_on=now_ist(),
                underlying=symbol,
                confidence=confidence,
                reason=f"[{expiry_type} {expiry}] Confidence {confidence.score}/{confidence.total}: "
                       + "; ".join(confidence.failed_reasons),
            ))
            continue

        suggestion_id = sug_repo.next_suggestion_id(trade_date)
        try:
            suggestion = assemble_suggestion(
                suggestion_id=suggestion_id,
                underlying=symbol,
                expiry=expiry,
                expiry_type=expiry_type,
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
            suggestions.append(suggestion)
            existing_names.append(suggestion.trade_name)  # prevent name collision between types
        except StrategyVeto as veto:
            no_suggestions.append(NoSuggestion(
                generated_on=now_ist(),
                underlying=symbol,
                confidence=confidence,
                reason=f"[{expiry_type} {expiry}] Strategy veto: {veto}",
            ))



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
        try:
            sugs, nss = _evaluate_underlying(db, symbol, trade_date)
        except Exception:
            logger.exception("Suggestion eval failed for %s", symbol)
            continue

        for sug in sugs:
            # Dedup guard — skip if already persisted this underlying+expiry_type today
            if sug_repo.has_suggestion_for(symbol, trade_date, sug.expiry_type):
                logger.info("Suggestion already exists for %s %s on %s -- skipping",
                            symbol, sug.expiry_type, trade_date)
                continue
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
            logger.info("Persisted suggestion %s (%s %s %s)",
                        sug.suggestion_id, sug.underlying, sug.expiry_type, sug.strategy)

        no_suggestions.extend(nss)

    import json as _json
    for ns in no_suggestions:
        try:
            ns_id = sug_repo.next_suggestion_id(trade_date)
            conditions = [
                {"label": c.label, "status": c.status, "detail": c.detail}
                for c in ns.confidence.checks
            ]
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
