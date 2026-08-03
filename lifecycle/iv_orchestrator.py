"""
lifecycle/iv_orchestrator.py
============================

For each (symbol, expiry, strike, option_type) on a trade date:
    1. Fetch market price from F&O EOD
    2. Fetch spot from spot EOD
    3. Compute IV (Black-Scholes bisection)
    4. Compute ATM IV per (symbol, expiry)
    5. Compute IV Rank (52w window) per (symbol, expiry, atm)
    6. Upsert into options_iv_history

When ``trade_date`` is omitted, recalculates IV for every weekday in the
lookback window where FO bhav exists but IV history is missing, and always
refreshes today when FO data is present.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, List, Optional

from config import STRATEGY_CONFIG
from database.connection import SQLServerConnection
from database.models import FoEodRepo, IvHistoryRepo, SpotEodRepo
from engine.iv_calculator import implied_vol
from engine.iv_rank import iv_percentile, iv_rank as compute_iv_rank, pick_atm_iv
from lifecycle.data_backfill import (
    backfill_lookback_days,
    run_dates_backfill,
    weekdays_in_range,
)
from lifecycle.eod_session import effective_bhav_end_date
from utils import days_between, today_ist

logger = logging.getLogger(__name__)


def _iv_dates_to_process(db: SQLServerConnection, end: Optional[date] = None) -> List[date]:
    """Weekdays with FO data but missing/stale IV, plus today when FO is ready."""
    end = end or effective_bhav_end_date()
    fo = FoEodRepo(db)
    iv = IvHistoryRepo(db)
    start = end - timedelta(days=backfill_lookback_days())
    pending: set[date] = set()
    for d in weekdays_in_range(start, end):
        if fo.has_trade_date(d) and not iv.has_trade_date(d):
            pending.add(d)
    if end.weekday() < 5 and fo.has_trade_date(end):
        pending.add(end)
    return sorted(pending)


def _run_iv_for_date(db: SQLServerConnection, trade_date: date) -> int:
    fo = FoEodRepo(db)
    sp = SpotEodRepo(db)
    iv_repo = IvHistoryRepo(db)

    if not fo.has_trade_date(trade_date):
        logger.warning("IV: no FO data for %s — skipping", trade_date)
        return 0

    total_rows = 0
    underlyings: List[str] = STRATEGY_CONFIG["underlyings"]

    for symbol in underlyings:
        spot_row = sp.for_date(symbol, trade_date)
        if not spot_row or float(spot_row["close_price"]) <= 0:
            logger.warning("IV: no spot for %s on %s", symbol, trade_date)
            continue
        spot = float(spot_row["close_price"])

        expiries = fo.expiries_for(symbol, trade_date)
        if not expiries:
            continue

        for expiry in expiries:
            dte = days_between(trade_date, expiry)
            if dte <= 0:
                continue

            chain = fo.get_chain(symbol, trade_date, expiry)
            if not chain:
                continue

            rows: List[Dict] = []
            triplets: List[tuple[float, str, float]] = []
            for r in chain:
                strike = float(r["strike"])
                opt_type = r["option_type"]
                market_price = float(r.get("settle_price") or r.get("close_price") or 0.0)
                if market_price <= 0:
                    continue
                iv, converged = implied_vol(
                    market_price=market_price,
                    spot=spot,
                    strike=strike,
                    days_to_expiry=dte,
                    option_type=opt_type,
                )
                if iv <= 0:
                    continue
                triplets.append((strike, opt_type, iv))
                rows.append({
                    "trade_date":   trade_date,
                    "symbol":       symbol,
                    "expiry_date":  expiry,
                    "strike":       strike,
                    "option_type":  opt_type,
                    "spot":         spot,
                    "market_price": market_price,
                    "iv":           iv,
                    "converged":    converged,
                    "atm_iv":       None,
                    "iv_rank":      None,
                    "iv_percentile": None,
                })

            if not rows:
                continue

            atm_iv = pick_atm_iv(triplets, spot)

            since = trade_date - timedelta(days=365)
            history_rows = iv_repo.atm_iv_history(symbol, since)
            history_values = [float(h["atm_iv"]) for h in history_rows
                              if h.get("atm_iv") is not None]

            ivr = compute_iv_rank(atm_iv or 0.0, history_values) if atm_iv else 0.0
            ivp = iv_percentile(atm_iv or 0.0, history_values) if atm_iv else 0.0

            for r in rows:
                r["atm_iv"] = atm_iv
                r["iv_rank"] = ivr
                r["iv_percentile"] = ivp

            n = iv_repo.upsert_many(rows)
            total_rows += n
            logger.info("IV: %s exp=%s rows=%d ATM_IV=%.4f IVR=%.1f",
                        symbol, expiry, n, atm_iv or 0.0, ivr)

    db.commit()
    logger.info("IV calc %s total rows: %d", trade_date, total_rows)
    return total_rows


def run_iv_calculation(
    db: SQLServerConnection,
    trade_date: date | None = None,
) -> int:
    if trade_date is not None:
        return _run_iv_for_date(db, trade_date)

    dates = _iv_dates_to_process(db)
    end = effective_bhav_end_date()
    return run_dates_backfill(
        dates,
        lambda d: _run_iv_for_date(db, d),
        label="IV calc",
        fail_if_today_missing=False,
        today=end,
    )
