"""
engine/market_data_provenance.py
================================

Derive ``data_as_of`` and ``live_data_freshness_ms`` from the rows that
actually priced a suggestion (spot + option chain), not from engine save time.

Rules
-----
* Only **pricing** rows count (live chain + spot). Structural EOD rows used
  for expiry lists, FII, or OI deltas do not move ``data_as_of``.
* Each row should carry ``_data_timestamp`` (naive IST) when the adapter
  knows it; otherwise we infer:
    - LIVE + ``_freshness_ms`` → ``now_ist() - freshness``
    - EOD + ``trade_date``     → that session's cash close (15:30 IST)
* ``live_data_freshness_ms`` is the **maximum** freshness among live pricing
  rows (oldest quote in the bundle — conservative for validation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, List, Optional, Set

from contracts import PricingProvenance
from utils import now_ist

# NSE cash session close — EOD bhav prices are for this instant on trade_date.
_EOD_SETTLE_TIME = time(15, 30)


def eod_settle_datetime(trade_date: date) -> datetime:
    """When settled EOD prices for ``trade_date`` are authoritative (IST, naive)."""
    return datetime.combine(trade_date, _EOD_SETTLE_TIME)


@dataclass
class PricingProvenanceTracker:
    """Accumulate pricing-row timestamps across spot + chain fetches."""

    _latest_ts: Optional[datetime] = field(default=None, init=False, repr=False)
    _max_freshness_ms: Optional[int] = field(default=None, init=False, repr=False)
    _sources: Set[str] = field(default_factory=set, init=False, repr=False)

    def observe_row(self, row: Optional[dict], *, role: str = "pricing") -> None:
        if not row or role != "pricing":
            return
        src = (row.get("_source") or "").upper()
        if src:
            self._sources.add(src)

        ts = _row_data_timestamp(row)
        if ts is not None:
            if self._latest_ts is None or ts > self._latest_ts:
                self._latest_ts = ts

        fresh = row.get("_freshness_ms")
        if fresh is not None:
            try:
                ms = int(fresh)
            except (TypeError, ValueError):
                ms = None
            if ms is not None:
                if self._max_freshness_ms is None or ms > self._max_freshness_ms:
                    self._max_freshness_ms = ms

    def observe_chain(self, rows: Optional[Iterable[dict]], *, role: str = "pricing") -> None:
        if not rows:
            return
        for row in rows:
            self.observe_row(row, role=role)

    def finalize(self) -> PricingProvenance:
        sources = self._sources
        if "LIVE" in sources and "EOD" in sources:
            ps = "MIXED"
        elif "LIVE" in sources:
            ps = "LIVE"
        elif "EOD" in sources:
            ps = "EOD"
        else:
            ps = "UNKNOWN"
        return PricingProvenance(
            data_as_of=self._latest_ts,
            live_data_freshness_ms=self._max_freshness_ms,
            pricing_source=ps,
        )


def _row_data_timestamp(row: dict) -> Optional[datetime]:
    """Resolve a single row's market-data instant (naive IST)."""
    raw = row.get("_data_timestamp")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", ""))
        except ValueError:
            pass

    src = (row.get("_source") or "").upper()
    fresh = row.get("_freshness_ms")
    if src == "LIVE" or fresh is not None:
        if fresh is not None:
            try:
                ms = int(fresh)
            except (TypeError, ValueError):
                ms = 0
            return now_ist() - timedelta(milliseconds=max(0, ms))
        return now_ist()

    td = row.get("trade_date")
    if isinstance(td, datetime):
        return eod_settle_datetime(td.date())
    if isinstance(td, date):
        return eod_settle_datetime(td)

    return None


def stamp_eod_rows(rows: List[dict], trade_date: date) -> List[dict]:
    """Tag FO/spot repo rows that lack provider provenance (in-process EOD reads)."""
    ts = eod_settle_datetime(trade_date)
    for r in rows:
        r.setdefault("_source", "EOD")
        r.setdefault("_data_timestamp", ts)
    return rows
