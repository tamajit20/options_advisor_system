"""Unit tests for lifecycle.eod_gap_replay."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from lifecycle import eod_gap_replay as replay


def _trade_row():
    return {
        "trade_id": "TRD-1",
        "suggestion_id": "SUG-1",
        "status": "ACTIVE",
        "executed_on": datetime(2026, 6, 16, 10, 0),
        "net_credit_actual": 2250.0,
        "actual_max_profit": 2250.0,
        "actual_max_loss": 5250.0,
        "actual_stop_loss_level": None,
    }


def _sug_legs():
    return [
        {
            "leg_order": 1,
            "symbol": "NIFTY",
            "expiry_date": date(2026, 7, 30),
            "strike": 23200.0,
            "option_type": "CE",
            "action": "SELL",
            "lots": 1,
            "lot_size": 75,
        },
        {
            "leg_order": 2,
            "symbol": "NIFTY",
            "expiry_date": date(2026, 7, 30),
            "strike": 23300.0,
            "option_type": "CE",
            "action": "BUY",
            "lots": 1,
            "lot_size": 75,
        },
    ]


def _trade_legs():
    return [
        {"leg_order": 1, "executed": 1, "fill_price": 50.0},
        {"leg_order": 2, "executed": 1, "fill_price": 20.0},
    ]


def _chain(short_mid: float, long_mid: float):
    return [
        {
            "strike": 23200.0,
            "option_type": "CE",
            "settle_price": short_mid,
            "close_price": short_mid,
        },
        {
            "strike": 23300.0,
            "option_type": "CE",
            "settle_price": long_mid,
            "close_price": long_mid,
        },
    ]


class TestEodGapReplay:
    def test_sl_hit_on_gap_day(self, mocker):
        db = MagicMock()
        trd = MagicMock()
        fo = MagicMock()
        mocker.patch("lifecycle.eod_gap_replay.TradeRepo", return_value=trd)
        mocker.patch("lifecycle.eod_gap_replay.FoEodRepo", return_value=fo)
        mocker.patch("lifecycle.eod_gap_replay.today_ist", return_value=date(2026, 6, 23))
        mocker.patch(
            "lifecycle.eod_gap_replay._latest_snapshot_at",
            return_value=datetime(2026, 6, 17, 15, 15),
        )

        trd.get.return_value = _trade_row()
        trd.legs.return_value = _trade_legs()
        db.fetch_all.return_value = _sug_legs()
        db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}

        def _chain_for(symbol, d, expiry):
            if d == date(2026, 6, 22):
                return _chain(200.0, 100.0)
            if d == date(2026, 6, 23):
                return _chain(210.0, 105.0)
            return []

        fo.get_chain.side_effect = _chain_for

        payload = replay.replay_gap_for_trade(db, "TRD-1")

        assert payload["has_gap"] is True
        assert payload["replay_from"] == "2026-06-18"
        assert len(payload["days"]) == 2
        assert payload["first_actionable"]["decision"] == "SL_HIT"
        assert payload["days"][0]["flags"] == ["sl_hit"]

    def test_no_gap_when_monitor_current(self, mocker):
        db = MagicMock()
        trd = MagicMock()
        fo = MagicMock()
        mocker.patch("lifecycle.eod_gap_replay.TradeRepo", return_value=trd)
        mocker.patch("lifecycle.eod_gap_replay.FoEodRepo", return_value=fo)
        mocker.patch("lifecycle.eod_gap_replay.today_ist", return_value=date(2026, 6, 17))
        mocker.patch(
            "lifecycle.eod_gap_replay._latest_snapshot_at",
            return_value=datetime(2026, 6, 17, 15, 15),
        )

        trd.get.return_value = _trade_row()
        trd.legs.return_value = _trade_legs()
        db.fetch_all.return_value = _sug_legs()
        db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        fo.get_chain.return_value = []

        payload = replay.replay_gap_for_trade(db, "TRD-1")

        assert payload["has_gap"] is False
        assert payload["days"] == []
        assert payload["replay_from"] is None

    def test_calendar_spread_mtm_uses_both_expiries(self, mocker):
        db = MagicMock()
        trd = MagicMock()
        fo = MagicMock()
        mocker.patch("lifecycle.eod_gap_replay.TradeRepo", return_value=trd)
        mocker.patch("lifecycle.eod_gap_replay.FoEodRepo", return_value=fo)
        mocker.patch("lifecycle.eod_gap_replay.today_ist", return_value=date(2026, 8, 18))
        mocker.patch(
            "lifecycle.eod_gap_replay._latest_snapshot_at",
            return_value=datetime(2026, 8, 17, 15, 15),
        )
        near, far = date(2026, 8, 25), date(2026, 9, 29)
        trd.get.return_value = {
            "trade_id": "TRD-CAL",
            "suggestion_id": "SUG-CAL",
            "status": "ACTIVE",
            "executed_on": datetime(2026, 8, 17, 10, 0),
            "net_credit_actual": -22456.0,
            "actual_max_profit": 18746.0,
            "actual_max_loss": 22456.0,
            "actual_stop_loss_level": None,
        }
        trd.legs.return_value = [
            {"leg_order": 1, "executed": 1, "fill_price": 535.6},
            {"leg_order": 2, "executed": 1, "fill_price": 1177.2},
        ]
        db.fetch_all.return_value = [
            {"leg_order": 1, "symbol": "BANKNIFTY", "expiry_date": near,
             "strike": 57600.0, "option_type": "CE", "action": "SELL",
             "lots": 1, "lot_size": 35},
            {"leg_order": 2, "symbol": "BANKNIFTY", "expiry_date": far,
             "strike": 57600.0, "option_type": "CE", "action": "BUY",
             "lots": 1, "lot_size": 35},
        ]
        db.fetch_one.return_value = {"strategy": "CALENDAR_SPREAD"}

        def _chain(_sym, d, expiry):
            if d != date(2026, 8, 18):
                return []
            if expiry == near:
                return [{"strike": 57600.0, "option_type": "CE",
                         "settle_price": 544.10, "close_price": 544.10}]
            if expiry == far:
                return [{"strike": 57600.0, "option_type": "CE",
                         "settle_price": 1200.0, "close_price": 1200.0}]
            return []

        fo.get_chain.side_effect = _chain
        payload = replay.replay_gap_for_trade(db, "TRD-CAL")
        assert payload["has_gap"] is True
        assert len(payload["days"]) == 1
        # Distinct expiries: MTM ≈ +500, not -22456.
        assert payload["days"][0]["mtm"] == pytest.approx(500.5, abs=1.0)
        assert payload["days"][0]["decision"] == "HOLD"
        assert "sl_hit" not in payload["days"][0]["flags"]


    def test_not_found(self, mocker):
        db = MagicMock()
        trd = MagicMock()
        mocker.patch("lifecycle.eod_gap_replay.TradeRepo", return_value=trd)
        trd.get.return_value = None
        assert replay.replay_gap_for_trade(db, "TRD-X") == {"error": "not_found"}
