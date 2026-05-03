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
from datetime import date, datetime, time, timedelta
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
    FiiRepo,
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
from utils import days_between, now_ist

logger = logging.getLogger(__name__)

# NSE market hours (IST)
_NSE_OPEN  = time(9, 15)
_NSE_CLOSE = time(15, 30)


def _resolve_data_date(db: SQLServerConnection) -> Optional[date]:
    """Return the most recent date for which BOTH FO bhav AND IV calculation
    are present and consistent.  This is the correct data date to use when
    the suggestion engine is triggered without an explicit trade_date.

    Logic:
    - fo_date  = MAX(trade_date) in options_fo_eod
    - iv_date  = MAX(trade_date) in options_iv_history
    - If both match              → use that date (the common case after nightly jobs)
    - If fo_date > iv_date       → IV hasn’t been computed for latest FO yet;
                                   fall back to iv_date (both tables agree there)
    - If iv_date > fo_date       → stale IV from a future date; use fo_date
    - If either is None          → return None (no data at all)
    """
    fo_date = FoEodRepo(db).latest_trade_date()
    iv_date = IvHistoryRepo(db).latest_trade_date()
    if fo_date is None:
        logger.warning(
            "_resolve_data_date: FO bhav table has no data — run fo_bhav_download first"
        )
        return None
    if iv_date is None:
        logger.warning(
            "_resolve_data_date: IV history table has no data — run iv_calculation first"
        )
        return None

    if fo_date == iv_date:
        logger.info(
            "_resolve_data_date: FO and IV both have data for %s — using this date",
            fo_date,
        )
        return fo_date

    chosen = min(fo_date, iv_date)
    if fo_date > iv_date:
        logger.info(
            "_resolve_data_date: FO has data up to %s but IV only computed up to %s. "
            "IV not yet run for %s. Falling back to %s (latest date where both agree).",
            fo_date, iv_date, fo_date, chosen,
        )
    else:
        logger.info(
            "_resolve_data_date: IV has data up to %s but FO only downloaded up to %s. "
            "Using %s (latest date where both agree).",
            iv_date, fo_date, chosen,
        )
    return chosen


def _execution_window(entry_day: date, now: datetime) -> str:
    """Build the execution window string.

    - If today IS the entry day AND we are currently inside market hours
      (09:15–15:30 IST) → 'Market is open — execute now at current market price'
    - Otherwise         → '09:20–09:45 IST {Weekday} DD-Mon-YY'
    """
    today = now.date()
    current_time = now.time()
    if today == entry_day and _NSE_OPEN <= current_time <= _NSE_CLOSE:
        return "Market is open — execute now at current market price"
    day_str = entry_day.strftime("%a %d-%b-%y")
    return f"09:20\u201309:45 IST {day_str}"


def _next_trading_day(d: date) -> date:
    """Return the next weekday after `d` (skips Sat/Sun).
    Used to compute entry DTE: suggestions generated on Friday evening
    are entered Monday morning (+3 cal days), not Tuesday (+1).
    NSE holidays are not considered here — DTE is approximate anyway.
    """
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:   # 5=Sat, 6=Sun
        nxt += timedelta(days=1)
    return nxt


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

    DTE is measured from the ENTRY day (next trading day after generation),
    not the generation day.  Mon-Thu: entry is next calendar day (+1).
    Friday: entry is Monday (+3 calendar days).
    """
    entry_day = _next_trading_day(trade_date)
    expiries = fo.expiries_for(symbol, trade_date)
    dte_min = STRATEGY_CONFIG["dte_min"]
    dte_max = STRATEGY_CONFIG["dte_max"]
    in_band = [e for e in expiries
               if dte_min <= days_between(entry_day, e) <= dte_max]
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
    entry_day: date,
    execution_window: str,
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

    spot_row = sp.for_date(symbol, trade_date)
    if not spot_row:
        logger.warning("Suggestion: no spot for %s on or before %s", symbol, trade_date)
        return [], []
    spot = float(spot_row["close_price"])
    actual_spot_date: Optional[date] = spot_row.get("trade_date")

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

    # FII net futures positioning — anchored to trade_date (never future data)
    fii_net_futures: Optional[float] = None
    actual_fii_date: Optional[date] = None
    try:
        fii_rows = FiiRepo(db).for_date(trade_date)
        fii_row = next((r for r in fii_rows if r.get("client_type") == "FII"), None)
        if fii_row:
            fii_net_futures = float(fii_row["future_long"]) - float(fii_row["future_short"])
            actual_fii_date = fii_row.get("trade_date")
    except Exception:
        logger.debug("FII net futures unavailable for %s (non-fatal)", symbol)

    # Capture the most recent VIX date used
    actual_vix_date: Optional[date] = (
        vix_history[-1].get("trade_date") if vix_history else None
    )

    iv_rows = iv_repo.latest_for(symbol, trade_date)

    suggestions: list[Suggestion] = []
    no_suggestions: list[NoSuggestion] = []

    for expiry, expiry_type in expiry_candidates:
        # entry_dte: calendar days from the actual entry day to expiry.
        # Mon-Thu generated → entered next day (+1); Fri generated → entered Monday (+3).
        entry_dte = max(days_between(entry_day, expiry), 0)

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
            dte=entry_dte,
            fii_net_futures=fii_net_futures,
        )

        confidence = evaluate_confidence(
            iv_rank=iv_rank,
            indicators=indicators,
            dte=entry_dte,
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
        primary_suggestion: Optional[Suggestion] = None
        try:
            primary_suggestion = assemble_suggestion(
                suggestion_id=suggestion_id,
                underlying=symbol,
                expiry=expiry,
                expiry_type=expiry_type,
                dte=entry_dte,
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
                execution_window=execution_window,
                data_date=trade_date,
                entry_date=entry_day,
                spot_data_date=actual_spot_date,
                fii_data_date=actual_fii_date,
                vix_data_date=actual_vix_date,
            )
            suggestions.append(primary_suggestion)
            existing_names.append(primary_suggestion.trade_name)
        except StrategyVeto as veto:
            no_suggestions.append(NoSuggestion(
                generated_on=now_ist(),
                underlying=symbol,
                confidence=confidence,
                reason=f"[{expiry_type} {expiry}] Strategy veto: {veto}",
            ))

        # When the primary is IC or IB, also generate BPS and BCS as cheaper
        # companion suggestions (half the margin — same put/call sides individually).
        #
        # INVARIANT: IC and IB are only ever selected when trend == "SIDEWAYS"
        # (enforced by select_strategy). If that ever changes this assertion will
        # catch it before a wrong suggestion is persisted.
        if primary_suggestion is not None and primary_suggestion.strategy in (
            "IRON_CONDOR", "IRON_BUTTERFLY"
        ):
            assert indicators.trend == "SIDEWAYS", (
                f"BUG: IC/IB generated for non-SIDEWAYS trend '{indicators.trend}' "
                f"on {symbol} — check strategy_selector.select_strategy()"
            )
            # Both BPS (put side) and BCS (call side) are valid for a SIDEWAYS market.
            for companion_strategy in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
                try:
                    comp_id = sug_repo.next_suggestion_id(trade_date)
                    comp = assemble_suggestion(
                        suggestion_id=comp_id,
                        underlying=symbol,
                        expiry=expiry,
                        expiry_type=expiry_type,
                        dte=entry_dte,
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
                        strategy_override=companion_strategy,
                        execution_window=execution_window,
                        data_date=trade_date,
                        entry_date=entry_day,
                        spot_data_date=actual_spot_date,
                        fii_data_date=actual_fii_date,
                        vix_data_date=actual_vix_date,
                    )
                    suggestions.append(comp)
                    existing_names.append(comp.trade_name)
                    logger.info("Companion suggestion %s (%s) generated alongside %s",
                                comp.trade_name, companion_strategy, primary_suggestion.trade_name)
                except StrategyVeto as veto:
                    logger.debug("Companion %s veto for %s %s: %s",
                                 companion_strategy, symbol, expiry, veto)

    return suggestions, no_suggestions  # always returns tuple — never None


def run_suggestion_engine(
    db: SQLServerConnection,
    trade_date: date | None = None,
) -> int:
    """Evaluate all configured underlyings and persist every one that passes
    all confidence checks (one suggestion per underlying per day). Underlyings
    that already have a suggestion for today are skipped so re-runs are safe.

    trade_date: the NSE bhav date to use for all data lookups.  When omitted
    the engine auto-detects the latest date for which both FO bhav AND IV
    have been computed (they must be consistent).  Pass an explicit date to
    re-run for a missed day or to back-test.

    Returns number of new suggestions persisted.
    """
    if trade_date is None:
        trade_date = _resolve_data_date(db)
        if trade_date is None:
            logger.warning("Suggestion engine: no consistent FO+IV data found — aborting")
            return 0
        logger.info("Suggestion engine: auto-resolved data date → %s", trade_date)
    else:
        logger.info("Suggestion engine: using explicit trade_date=%s", trade_date)

    # Execution window: when/how to enter this trade.
    # Use max(trade_date, today) so that a weekend/late run with stale data
    # still produces an entry_day in the future (e.g. Sun run with Thu data
    # → entry_day = Monday, not the already-passed Friday).
    _today = now_ist().date()
    entry_day = _next_trading_day(max(trade_date, _today))
    exec_window = _execution_window(entry_day, now_ist())
    sug_repo = SuggestionRepo(db)
    notif_repo = NotificationRepo(db)

    # Collect all candidates across all underlyings first, then apply
    # cross-underlying dedup before persisting.
    all_candidates: List[Suggestion] = []
    no_suggestions: List[NoSuggestion] = []

    for symbol in STRATEGY_CONFIG["underlyings"]:
        try:
            sugs, nss = _evaluate_underlying(db, symbol, trade_date, entry_day, exec_window)
        except Exception:
            logger.exception("Suggestion eval failed for %s", symbol)
            continue
        all_candidates.extend(sugs)
        no_suggestions.extend(nss)

    # ── Cross-underlying dedup ─────────────────────────────────────────────
    # NIFTY, BANKNIFTY, FINNIFTY are 85–95% correlated; three simultaneous
    # suggestions of the same expiry type + strategy is one concentrated bet.
    # Keep only the highest-confidence suggestion per (expiry_type, strategy) group.
    # Companion spreads (BPS, BCS) are keyed separately so IC + BPS can coexist.
    best_by_expiry_type: dict[str, Suggestion] = {}
    for sug in all_candidates:
        key = f"{sug.expiry_type}:{sug.strategy}"
        existing = best_by_expiry_type.get(key)
        is_better = (
            existing is None
            or sug.confidence.score > existing.confidence.score
            or (
                sug.confidence.score == existing.confidence.score
                and (sug.confidence.score or 0) >= (existing.confidence.score or 0)
            )
        )
        if is_better:
            if existing is not None:
                logger.info(
                    "Cross-underlying dedup: dropping %s (%s, score=%d) in favour of %s (%s, score=%d)",
                    existing.underlying, key, existing.confidence.score,
                    sug.underlying, key, sug.confidence.score,
                )
                no_suggestions.append(NoSuggestion(
                    generated_on=now_ist(),
                    underlying=existing.underlying,
                    confidence=existing.confidence,
                    reason=(
                        f"[{key}] Dropped — correlation dedup: "
                        f"{sug.underlying} has equal/higher confidence score "
                        f"({sug.confidence.score} vs {existing.confidence.score})"
                    ),
                ))
            best_by_expiry_type[key] = sug

    persisted: List[Suggestion] = []

    for sug in best_by_expiry_type.values():
        # Retire any PENDING suggestion for the same slot with an older entry_date.
        # This covers the case where data_date is unchanged (e.g. weekend re-run)
        # but entry_day has moved forward (Fri→Mon becomes the new execution day).
        expired = sug_repo.expire_stale_pending(
            sug.underlying, sug.expiry_type, sug.strategy, entry_day
        )
        if expired:
            logger.info(
                "Expired %d stale PENDING suggestion(s) for %s %s — new entry day is %s",
                expired, sug.underlying, sug.expiry_type, entry_day,
            )

        # Dedup: skip only if a suggestion for this EXACT entry_date already exists
        if sug_repo.has_suggestion_for(
            sug.underlying, trade_date, sug.expiry_type, sug.strategy,
            entry_date=entry_day,
        ):
            logger.info(
                "Suggestion already exists for %s %s entry_date=%s — skipping",
                sug.underlying, sug.expiry_type, entry_day,
            )
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
