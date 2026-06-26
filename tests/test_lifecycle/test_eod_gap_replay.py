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

    def test_not_found(self, mocker):
        db = MagicMock()
        trd = MagicMock()
        mocker.patch("lifecycle.eod_gap_replay.TradeRepo", return_value=trd)
        trd.get.return_value = None
        assert replay.replay_gap_for_trade(db, "TRD-X") == {"error": "not_found"}
