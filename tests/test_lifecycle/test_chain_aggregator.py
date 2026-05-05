"""Unit tests for lifecycle.chain_aggregator — 5-min WS chain aggregator."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from contracts import ChainTrajectory
from lifecycle.chain_aggregator import (
    ChainTickAggregator,
    _floor_to_5min,
    _next_5min_boundary,
    load_trajectory,
)
from providers.base import DataSource, LiveQuote


def _q(symbol, expiry, strike, ot, last, *, oi=None, vol=None, bid=None, ask=None, ts=None):
    return LiveQuote(
        symbol=symbol, expiry=expiry, strike=strike, option_type=ot,
        last_price=last, bid=bid, ask=ask, volume=vol, open_interest=oi,
        timestamp=ts, source=DataSource.LIVE, provider="zerodha",
    )


def _spot(symbol, last, ts=None):
    return LiveQuote(
        symbol=symbol, expiry=None, strike=None, option_type=None,
        last_price=last, timestamp=ts, source=DataSource.LIVE, provider="zerodha",
    )


class TestBoundaryHelpers:
    def test_floor_to_5min(self):
        assert _floor_to_5min(datetime(2026, 5, 5, 10, 27, 33)) == datetime(2026, 5, 5, 10, 25)
        assert _floor_to_5min(datetime(2026, 5, 5, 10, 30, 0)) == datetime(2026, 5, 5, 10, 30)

    def test_next_5min_boundary(self):
        assert _next_5min_boundary(datetime(2026, 5, 5, 10, 27, 0)) == datetime(2026, 5, 5, 10, 30)
        assert _next_5min_boundary(datetime(2026, 5, 5, 10, 30, 0)) == datetime(2026, 5, 5, 10, 35)


class TestAggregator:
    def test_flush_persists_chain_aggregate(self, mock_db):
        agg = ChainTickAggregator(
            db=mock_db,
            expiry_provider=lambda s: [],
            event_bus=MagicMock(),
        )
        exp = date(2026, 5, 28)

        # Spot first so ATM resolves
        agg._on_tick(_spot("NIFTY", 23000.0))
        # CE+PE at three strikes
        agg._on_tick(_q("NIFTY", exp, 22900.0, "CE", 105.0, oi=100_000, vol=10_000, bid=104.5, ask=105.5))
        agg._on_tick(_q("NIFTY", exp, 22900.0, "PE", 50.0, oi=80_000, vol=8_000, bid=49.5, ask=50.5))
        agg._on_tick(_q("NIFTY", exp, 23000.0, "CE", 75.0, oi=200_000, vol=20_000, bid=74.5, ask=75.5))
        agg._on_tick(_q("NIFTY", exp, 23000.0, "PE", 75.0, oi=150_000, vol=18_000, bid=74.5, ask=75.5))
        agg._on_tick(_q("NIFTY", exp, 23100.0, "CE", 50.0, oi=120_000, vol=11_000, bid=49.5, ask=50.5))
        agg._on_tick(_q("NIFTY", exp, 23100.0, "PE", 110.0, oi=90_000, vol=9_000, bid=109.5, ask=110.5))

        # Tick again with higher OI/volume so deltas are non-zero.
        agg._on_tick(_q("NIFTY", exp, 22900.0, "CE", 106.0, oi=105_000, vol=12_000, bid=105.5, ask=106.5))
        agg._on_tick(_q("NIFTY", exp, 22900.0, "PE", 49.0, oi=82_000, vol=8_500, bid=48.5, ask=49.5))

        snap_at = datetime(2026, 5, 5, 10, 30)
        agg.flush_at(snap_at)

        # Verify INSERT into options_chain_5min happened
        calls = [c for c in mock_db.execute.call_args_list
                 if "INSERT INTO options_chain_5min" in c.args[0]]
        assert len(calls) == 1
        params = calls[0].args[1]
        # snapshot_at, symbol, expiry_date, spot, atm_strike, sum_call_oi, sum_put_oi, ...
        assert params[0] == snap_at
        assert params[1] == "NIFTY"
        assert params[2] == exp
        assert params[3] == 23000.0           # spot
        assert params[4] == 23000.0           # ATM strike
        assert params[5] == 425_000           # sum_call_oi (105k+200k+120k)
        assert params[6] == 322_000           # sum_put_oi  (82k+150k+90k, latest tick at 22900PE oi=82k)
        # ATM mid (CE & PE at 23000): (74.5+75.5)/2 = 75.0
        assert params[11] == 75.0             # atm_call_mid
        assert params[12] == 75.0             # atm_put_mid
        # Spread bps: (75.5-74.5)/75.0 * 10_000 ≈ 133.33 bps
        assert abs(params[13] - 133.33) < 0.5

    def test_atm_iv_row_inserted_when_premiums_present(self, mock_db):
        agg = ChainTickAggregator(
            db=mock_db,
            expiry_provider=lambda s: [],
            event_bus=MagicMock(),
        )
        exp = date(2026, 5, 28)
        agg._on_tick(_spot("NIFTY", 23000.0))
        # ATM CE+PE with realistic premiums
        agg._on_tick(_q("NIFTY", exp, 23000.0, "CE", 200.0, bid=199.0, ask=201.0, oi=200_000, vol=20_000))
        agg._on_tick(_q("NIFTY", exp, 23000.0, "PE", 200.0, bid=199.0, ask=201.0, oi=150_000, vol=15_000))

        # Use a snapshot date that yields positive DTE.
        snap_at = datetime(2026, 5, 5, 10, 30)
        agg.flush_at(snap_at)

        iv_calls = [c for c in mock_db.execute.call_args_list
                    if "INSERT INTO options_atm_iv_5min" in c.args[0]]
        assert len(iv_calls) == 1
        params = iv_calls[0].args[1]
        assert params[1] == "NIFTY"
        assert params[2] == exp
        assert params[3] == 23000.0    # atm_strike
        assert params[6] is not None    # atm_iv computed
        assert 0.05 < params[6] < 1.0   # sanity: annualised vol in plausible range

    def test_window_rebaseline_on_flush(self, mock_db):
        agg = ChainTickAggregator(
            db=mock_db,
            expiry_provider=lambda s: [],
            event_bus=MagicMock(),
        )
        exp = date(2026, 5, 28)
        agg._on_tick(_spot("NIFTY", 23000.0))
        agg._on_tick(_q("NIFTY", exp, 23000.0, "CE", 75.0, oi=200_000, vol=20_000, bid=74.5, ask=75.5))
        agg._on_tick(_q("NIFTY", exp, 23000.0, "PE", 75.0, oi=150_000, vol=18_000, bid=74.5, ask=75.5))

        agg.flush_at(datetime(2026, 5, 5, 10, 30))

        # Bucket window-start should now match latest snapshot.
        b = agg._buckets[("NIFTY", exp, 23000.0, "CE")]
        assert b.open_interest_at_window_start == 200_000
        assert b.volume_at_window_start == 20_000
        assert b.sample_count == 0


class TestLoadTrajectory:
    def test_returns_empty_trajectory_when_no_rows(self, mock_db):
        mock_db.fetch_all.return_value = []
        traj = load_trajectory(mock_db, symbol="NIFTY", expiry=date(2026, 5, 28),
                                now=datetime(2026, 5, 5, 11, 0))
        assert isinstance(traj, ChainTrajectory)
        assert traj.oi_pcr_change_series == []
        assert traj.atm_iv_series == []
        assert traj.latest_call_spread_bps is None

    def test_builds_oi_pcr_series_from_deltas(self, mock_db):
        # First fetch (chain rows), then second (IV rows). Rows are returned in
        # DESC order then reversed by recent_window — so we feed them DESC here.
        chain_rows = [
            {"snapshot_at": datetime(2026, 5, 5, 10, 30),
             "sum_call_oi_delta": 1000, "sum_put_oi_delta": 2000,
             "sum_call_volume": 500, "sum_put_volume": 700,
             "atm_call_spread_bps": 25.0, "atm_put_spread_bps": 30.0},
            {"snapshot_at": datetime(2026, 5, 5, 10, 25),
             "sum_call_oi_delta": 2000, "sum_put_oi_delta": 4000,
             "sum_call_volume": 400, "sum_put_volume": 800,
             "atm_call_spread_bps": 25.0, "atm_put_spread_bps": 30.0},
            {"snapshot_at": datetime(2026, 5, 5, 10, 20),
             "sum_call_oi_delta": 3000, "sum_put_oi_delta": 6000,
             "sum_call_volume": 300, "sum_put_volume": 900,
             "atm_call_spread_bps": 25.0, "atm_put_spread_bps": 30.0},
        ]
        iv_rows = [
            {"snapshot_at": datetime(2026, 5, 5, 10, 30), "atm_iv": 0.18},
            {"snapshot_at": datetime(2026, 5, 5, 10, 25), "atm_iv": 0.17},
            {"snapshot_at": datetime(2026, 5, 5, 10, 20), "atm_iv": 0.16},
        ]
        mock_db.fetch_all.side_effect = [chain_rows, iv_rows]
        traj = load_trajectory(mock_db, symbol="NIFTY", expiry=date(2026, 5, 28),
                                now=datetime(2026, 5, 5, 11, 0))
        # After reversal, oldest first.
        assert traj.oi_pcr_change_series == [2.0, 2.0, 2.0]
        assert traj.atm_iv_series == [0.16, 0.17, 0.18]
        assert traj.latest_call_spread_bps == 25.0
        assert traj.latest_put_spread_bps == 30.0
