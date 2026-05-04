"""
lifecycle/snapshot_orchestrator.py
==================================

Phase 2b.1 — Two scheduled jobs that work as a pair:

* `run_intraday_close_snapshot` (15:35 IST) — for every leg of every ACTIVE
  trade, capture the current LTP via the live `MarketDataProvider` (which
  silently falls back to EOD when Zerodha is degraded). Persist into
  `options_intraday_close_snapshot`. Tiny by volume (~5 KB/day at full
  open-trade load).

* `run_drift_verifier` (19:35 IST, after `fo_bhav_download` at 18:30) —
  load today's snapshot rows, compare each LTP to the matching settled
  close in `options_fo_eod`, and fire a `DRIFT_WARNING` notification
  (severity WARNING) for any leg whose drift exceeds
  `STRATEGY_CONFIG["intraday_close_drift_pct"]` (default 5%). Rows whose
  source was already 'EOD' at 15:35 are skipped because the comparison
  would be trivially zero.

Why two phases?
    A consistent drift between live LTP at 15:35 and the official settled
    close (computed by the exchange ~18:00 IST) is the cleanest available
    signal that the live-data feed has gone subtly wrong (stale ticks,
    instrument-master mismatch, segment off, etc.). Fixing those issues
    quickly matters because suggestion / exit engines downstream act on
    live prices during market hours.

Both jobs are best-effort:
    * Provider failures fall back to EOD prices (already the default
      behaviour of `ZerodhaProvider`); the source/freshness markers tell
      the verifier whether the row is comparable.
    * Notification dispatch failures are swallowed — the snapshot rows
      are the durable record.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Iterable, List, Optional

from config import STRATEGY_CONFIG
from contracts import Notification
from database.connection import SQLServerConnection
from database.models import (
    FoEodRepo,
    IntradayCloseSnapshotRepo,
    NotificationRepo,
    TradeRepo,
)
from providers.base import DataSource, MarketDataProvider
from providers.registry import get_market_data
from utils import now_ist, today_ist

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _chain_index(rows: Iterable[dict]) -> Dict[tuple, dict]:
    """Index a chain (list of dicts) by (strike, option_type) for O(1) leg
    lookup. Strikes are coerced to float to dodge Decimal-vs-float key
    mismatches between providers."""
    idx: Dict[tuple, dict] = {}
    for r in rows or []:
        try:
            key = (float(r["strike"]), str(r["option_type"]).upper())
        except (KeyError, ValueError, TypeError):
            continue
        idx[key] = r
    return idx


def _row_ltp(row: dict) -> Optional[float]:
    """Extract a usable LTP from a chain row. Live providers populate
    `last_price`; EOD rows fall back to settle_price/close_price."""
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


# ---------------------------------------------------------------------------
# Job 1 — capture live LTP at 15:35 IST
# ---------------------------------------------------------------------------
def run_intraday_close_snapshot(
    db: SQLServerConnection,
    trade_date: Optional[date] = None,
    *,
    provider: Optional[MarketDataProvider] = None,
) -> int:
    """Capture LTP for every leg of every ACTIVE trade and persist.

    Returns the number of leg-rows written. Tests inject `provider`; in
    production we read from the registry (which is process-cached).
    """
    snapshot_date = trade_date or today_ist()
    p = provider if provider is not None else get_market_data()
    captured_at = now_ist()

    trades = TradeRepo(db).open_trades()
    if not trades:
        logger.info("intraday_close_snapshot: no ACTIVE trades — nothing to capture")
        return 0

    rows: List[dict] = []
    chains_cache: Dict[tuple, Dict[tuple, dict]] = {}

    for trade in trades:
        trade_id = trade["trade_id"]
        sug_legs = db.fetch_all(
            "SELECT leg_order, symbol, expiry_date, strike, option_type "
            "FROM options_suggestion_legs WHERE suggestion_id = ? ORDER BY leg_order",
            [trade["suggestion_id"]],
        )
        for sl in sug_legs:
            symbol = sl["symbol"]
            expiry = sl["expiry_date"]
            chain_key = (symbol, expiry)
            if chain_key not in chains_cache:
                try:
                    chain_rows = p.get_chain(symbol, snapshot_date, expiry)
                except Exception as exc:
                    logger.warning(
                        "intraday_close_snapshot: get_chain(%s,%s) failed: %s",
                        symbol, expiry, exc,
                    )
                    chain_rows = []
                chains_cache[chain_key] = _chain_index(chain_rows)
            idx = chains_cache[chain_key]
            row = idx.get((float(sl["strike"]), str(sl["option_type"]).upper()))
            if row is None:
                ltp = None
                source = None
                provider_name = None
                freshness = None
            else:
                ltp = _row_ltp(row)
                source = row.get("_source")
                provider_name = row.get("_provider")
                freshness = row.get("_freshness_ms")

            rows.append({
                "snapshot_date": snapshot_date,
                "captured_at":   captured_at,
                "trade_id":      trade_id,
                "leg_order":     int(sl["leg_order"]),
                "symbol":        symbol,
                "expiry_date":   expiry,
                "strike":        float(sl["strike"]),
                "option_type":   str(sl["option_type"]).upper(),
                "ltp":           ltp,
                "source":        source,
                "provider":      provider_name,
                "freshness_ms":  freshness,
            })

    n = IntradayCloseSnapshotRepo(db).insert_many(rows)
    logger.info(
        "intraday_close_snapshot: captured %d legs across %d trades",
        n, len(trades),
    )
    return n


# ---------------------------------------------------------------------------
# Job 2 — verify live LTP vs settled close at 19:35 IST
# ---------------------------------------------------------------------------
def run_drift_verifier(
    db: SQLServerConnection,
    trade_date: Optional[date] = None,
) -> int:
    """Compare today's snapshot rows to settled close prices and fire a
    `DRIFT_WARNING` notification for any leg that drifted more than the
    configured threshold.

    Returns the number of legs flagged. Snapshot rows whose source is
    `EOD` are skipped (the comparison is trivially zero).
    """
    snapshot_date = trade_date or today_ist()
    threshold_pct = float(STRATEGY_CONFIG.get("intraday_close_drift_pct", 5.0))

    snaps = IntradayCloseSnapshotRepo(db).get_by_date(snapshot_date)
    if not snaps:
        logger.info("drift_verifier: no snapshot for %s — skipping", snapshot_date)
        return 0

    fo = FoEodRepo(db)
    notif = NotificationRepo(db)
    chains_cache: Dict[tuple, Dict[tuple, dict]] = {}

    drifted_legs: List[dict] = []
    for s in snaps:
        # Skip rows where the snapshot itself was already EOD — comparison is
        # trivially identical and would only generate noise.
        if (s.get("source") or "").upper() == DataSource.EOD.value.upper():
            continue
        live_ltp = s.get("ltp")
        if live_ltp is None:
            continue
        try:
            live_f = float(live_ltp)
        except (TypeError, ValueError):
            continue
        if live_f <= 0:
            continue

        symbol = s["symbol"]
        expiry = s["expiry_date"]
        chain_key = (symbol, expiry)
        if chain_key not in chains_cache:
            chain_rows = fo.get_chain(symbol, snapshot_date, expiry)
            chains_cache[chain_key] = _chain_index(chain_rows)
        idx = chains_cache[chain_key]
        row = idx.get((float(s["strike"]), str(s["option_type"]).upper()))
        if row is None:
            continue
        settled = _row_ltp(row)
        if settled is None or settled <= 0:
            continue

        drift_pct = abs(live_f - settled) / settled * 100.0
        if drift_pct > threshold_pct:
            drifted_legs.append({
                "trade_id":   s["trade_id"],
                "leg_order":  s["leg_order"],
                "symbol":     symbol,
                "expiry":     expiry,
                "strike":     float(s["strike"]),
                "option_type": s["option_type"],
                "live":       live_f,
                "settled":    settled,
                "drift_pct":  drift_pct,
            })

    if not drifted_legs:
        logger.info(
            "drift_verifier: %d snapshot rows checked, no drift > %.2f%%",
            len(snaps), threshold_pct,
        )
        return 0

    # Build a single rolled-up notification per run. One alert is more
    # actionable than N legs of noise.
    body_lines = [
        f"{d['trade_id']} leg#{d['leg_order']} {d['symbol']} "
        f"{d['expiry']:%Y-%m-%d} {d['strike']:g}{d['option_type']}: "
        f"live={d['live']:.2f} settled={d['settled']:.2f} "
        f"drift={d['drift_pct']:.2f}%"
        for d in drifted_legs
    ]
    body = "\n".join(body_lines)
    title = (
        f"Live-vs-settled drift on {snapshot_date}: "
        f"{len(drifted_legs)} leg(s) > {threshold_pct:g}%"
    )
    try:
        notif.insert(Notification(
            created_at=now_ist(),
            notif_type="DRIFT_WARNING",
            severity="WARNING",
            title=title,
            body=body[:4000],
        ))
    except Exception:
        logger.exception("drift_verifier: notification persistence failed")

    logger.warning(
        "drift_verifier: %d legs drifted > %.2f%% on %s",
        len(drifted_legs), threshold_pct, snapshot_date,
    )
    return len(drifted_legs)
