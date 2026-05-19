"""Tests for engine/market_data_provenance.py"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from engine.market_data_provenance import (
    PricingProvenanceTracker,
    eod_settle_datetime,
    stamp_eod_rows,
)
from utils import now_ist


class TestEodSettle:
    def test_eod_settle_is_1530_on_trade_date(self):
        d = date(2026, 5, 4)
        assert eod_settle_datetime(d) == datetime(2026, 5, 4, 15, 30, 0)


class TestPricingProvenanceTracker:
    def test_live_spot_and_chain_use_latest_timestamp(self):
        ts = now_ist() - timedelta(seconds=12)
        tracker = PricingProvenanceTracker()
        tracker.observe_row({
            "_source": "LIVE",
            "_data_timestamp": ts,
            "_freshness_ms": 12_000,
        })
        tracker.observe_chain([
            {
                "_source": "LIVE",
                "_data_timestamp": ts + timedelta(seconds=3),
                "_freshness_ms": 5_000,
            },
        ])
        prov = tracker.finalize()
        assert prov.pricing_source == "LIVE"
        assert prov.data_as_of == ts + timedelta(seconds=3)
        assert prov.live_data_freshness_ms == 12_000

    def test_eod_chain_from_trade_date(self):
        td = date(2026, 5, 2)
        tracker = PricingProvenanceTracker()
        tracker.observe_chain(stamp_eod_rows([{"trade_date": td, "strike": 23000}], td))
        prov = tracker.finalize()
        assert prov.pricing_source == "EOD"
        assert prov.data_as_of == eod_settle_datetime(td)

    def test_mixed_spot_eod_chain_live(self):
        tracker = PricingProvenanceTracker()
        tracker.observe_row({
            "_source": "EOD",
            "_data_timestamp": eod_settle_datetime(date(2026, 5, 3)),
        })
        tracker.observe_row({
            "_source": "LIVE",
            "_data_timestamp": now_ist(),
            "_freshness_ms": 100,
        })
        prov = tracker.finalize()
        assert prov.pricing_source == "MIXED"

    def test_structural_rows_ignored(self):
        tracker = PricingProvenanceTracker()
        tracker.observe_chain(
            stamp_eod_rows([{"trade_date": date(2026, 5, 1)}], date(2026, 5, 1)),
            role="structural",
        )
        prov = tracker.finalize()
        assert prov.data_as_of is None
        assert prov.pricing_source == "UNKNOWN"
