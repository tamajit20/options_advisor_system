"""
tests/test_lifecycle/test_snapshot_orchestrator.py
==================================================

Phase 2b.1 — 15:35 IST live-LTP capture and 19:35 IST drift verifier.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

import lifecycle.snapshot_orchestrator as orch


_TODAY = date(2026, 5, 4)
_EXPIRY = date(2026, 5, 14)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _open_trade(trade_id: str = "TRD-1", suggestion_id: str = "SUG-1") -> dict:
    return {"trade_id": trade_id, "suggestion_id": suggestion_id, "status": "ACTIVE"}


def _sug_legs() -> list[dict]:
    return [
        {"leg_order": 1, "symbol": "NIFTY", "expiry_date": _EXPIRY,
         "strike": 23200.0, "option_type": "CE"},
        {"leg_order": 2, "symbol": "NIFTY", "expiry_date": _EXPIRY,
         "strike": 23300.0, "option_type": "CE"},
    ]


def _live_chain() -> list[dict]:
    return [
        {"strike": 23200.0, "option_type": "CE", "last_price": 50.0,
         "_source": "LIVE", "_provider": "zerodha", "_freshness_ms": 250},
        {"strike": 23300.0, "option_type": "CE", "last_price": 22.0,
         "_source": "LIVE", "_provider": "zerodha", "_freshness_ms": 250},
    ]


def _settled_chain() -> list[dict]:
    """Settled close that's CLOSE to the live captures (no drift)."""
    return [
        {"strike": 23200.0, "option_type": "CE", "settle_price": 49.5},
        {"strike": 23300.0, "option_type": "CE", "settle_price": 22.2},
    ]


def _settled_chain_drifted() -> list[dict]:
    """Settled close that's FAR from the live captures (>5% drift on leg 1)."""
    return [
        # live=50, settled=40 → drift 25%
        {"strike": 23200.0, "option_type": "CE", "settle_price": 40.0},
        # live=22, settled=21.8 → drift 0.9%
        {"strike": 23300.0, "option_type": "CE", "settle_price": 21.8},
    ]


# ---------------------------------------------------------------------------
# 15:35 snapshot job
# ---------------------------------------------------------------------------
class TestRunIntradayCloseSnapshot:
    def test_no_open_trades_writes_nothing(self, mock_db, mocker):
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[])
        provider = MagicMock()
        n = orch.run_intraday_close_snapshot(mock_db, _TODAY, provider=provider)
        assert n == 0
        provider.get_chain.assert_not_called()

    def test_captures_one_row_per_leg(self, mock_db, mocker):
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mock_db.fetch_all.return_value = _sug_legs()
        provider = MagicMock()
        provider.get_chain.return_value = _live_chain()
        insert_many = mocker.patch.object(
            orch.IntradayCloseSnapshotRepo, "insert_many", return_value=2,
        )
        n = orch.run_intraday_close_snapshot(mock_db, _TODAY, provider=provider)
        assert n == 2
        # Only ONE chain fetch even with two legs (same symbol+expiry)
        provider.get_chain.assert_called_once_with("NIFTY", _TODAY, _EXPIRY)
        rows = insert_many.call_args[0][0]
        assert len(rows) == 2
        assert rows[0]["ltp"] == 50.0
        assert rows[0]["source"] == "LIVE"
        assert rows[0]["provider"] == "zerodha"
        assert rows[0]["snapshot_date"] == _TODAY
        assert rows[0]["trade_id"] == "TRD-1"
        assert rows[1]["ltp"] == 22.0

    def test_missing_chain_row_yields_null_ltp(self, mock_db, mocker):
        """Provider returns chain that's missing leg 2 → that leg's ltp=None."""
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mock_db.fetch_all.return_value = _sug_legs()
        provider = MagicMock()
        # Only leg 1 strike 23200 returned
        provider.get_chain.return_value = [
            {"strike": 23200.0, "option_type": "CE", "last_price": 50.0,
             "_source": "LIVE", "_provider": "zerodha"},
        ]
        insert_many = mocker.patch.object(
            orch.IntradayCloseSnapshotRepo, "insert_many", return_value=2,
        )
        orch.run_intraday_close_snapshot(mock_db, _TODAY, provider=provider)
        rows = insert_many.call_args[0][0]
        assert rows[0]["ltp"] == 50.0
        assert rows[1]["ltp"] is None
        assert rows[1]["source"] is None

    def test_provider_failure_falls_through(self, mock_db, mocker):
        """get_chain raising must not crash the job; legs get null LTPs."""
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mock_db.fetch_all.return_value = _sug_legs()
        provider = MagicMock()
        provider.get_chain.side_effect = RuntimeError("upstream blew up")
        insert_many = mocker.patch.object(
            orch.IntradayCloseSnapshotRepo, "insert_many", return_value=2,
        )
        n = orch.run_intraday_close_snapshot(mock_db, _TODAY, provider=provider)
        assert n == 2
        rows = insert_many.call_args[0][0]
        assert all(r["ltp"] is None for r in rows)


# ---------------------------------------------------------------------------
# 19:35 drift verifier
# ---------------------------------------------------------------------------
class TestRunDriftVerifier:
    def test_no_snapshot_returns_zero(self, mock_db, mocker):
        mocker.patch.object(orch.IntradayCloseSnapshotRepo, "get_by_date",
                            return_value=[])
        n = orch.run_drift_verifier(mock_db, _TODAY)
        assert n == 0

    def test_no_drift_no_notification(self, mock_db, mocker):
        snaps = _capture_rows()
        mocker.patch.object(orch.IntradayCloseSnapshotRepo, "get_by_date",
                            return_value=snaps)
        mocker.patch.object(orch.FoEodRepo, "get_chain",
                            return_value=_settled_chain())
        notif_insert = mocker.patch.object(orch.NotificationRepo, "insert")
        n = orch.run_drift_verifier(mock_db, _TODAY)
        assert n == 0
        notif_insert.assert_not_called()

    def test_drift_above_threshold_fires_one_notification(self, mock_db, mocker):
        snaps = _capture_rows()
        mocker.patch.object(orch.IntradayCloseSnapshotRepo, "get_by_date",
                            return_value=snaps)
        mocker.patch.object(orch.FoEodRepo, "get_chain",
                            return_value=_settled_chain_drifted())
        notif_insert = mocker.patch.object(orch.NotificationRepo, "insert")
        n = orch.run_drift_verifier(mock_db, _TODAY)
        # Only leg 1 drifted (25%); leg 2 is within tolerance.
        assert n == 1
        notif_insert.assert_called_once()
        notif = notif_insert.call_args[0][0]
        assert notif.notif_type == "DRIFT_WARNING"
        assert notif.severity == "WARNING"
        assert "drift" in notif.title.lower()
        assert "TRD-1" in notif.body

    def test_eod_source_rows_skipped(self, mock_db, mocker):
        """Rows captured when provider had already fallen back to EOD must
        not be compared (drift would be trivially zero — useless noise)."""
        snaps = _capture_rows()
        for s in snaps:
            s["source"] = "EOD"
        mocker.patch.object(orch.IntradayCloseSnapshotRepo, "get_by_date",
                            return_value=snaps)
        get_chain = mocker.patch.object(orch.FoEodRepo, "get_chain",
                                        return_value=_settled_chain_drifted())
        notif_insert = mocker.patch.object(orch.NotificationRepo, "insert")
        n = orch.run_drift_verifier(mock_db, _TODAY)
        assert n == 0
        get_chain.assert_not_called()
        notif_insert.assert_not_called()

    def test_null_ltp_rows_skipped(self, mock_db, mocker):
        snaps = _capture_rows()
        for s in snaps:
            s["ltp"] = None
        mocker.patch.object(orch.IntradayCloseSnapshotRepo, "get_by_date",
                            return_value=snaps)
        mocker.patch.object(orch.FoEodRepo, "get_chain",
                            return_value=_settled_chain_drifted())
        notif_insert = mocker.patch.object(orch.NotificationRepo, "insert")
        n = orch.run_drift_verifier(mock_db, _TODAY)
        assert n == 0
        notif_insert.assert_not_called()

    def test_threshold_is_configurable(self, mock_db, mocker, monkeypatch):
        """A 0.1% threshold should flag legs that 5% wouldn't."""
        monkeypatch.setitem(orch.STRATEGY_CONFIG, "intraday_close_drift_pct", 0.1)
        snaps = _capture_rows()
        mocker.patch.object(orch.IntradayCloseSnapshotRepo, "get_by_date",
                            return_value=snaps)
        mocker.patch.object(orch.FoEodRepo, "get_chain",
                            return_value=_settled_chain())  # ~1% drift on leg 1
        notif_insert = mocker.patch.object(orch.NotificationRepo, "insert")
        n = orch.run_drift_verifier(mock_db, _TODAY)
        assert n >= 1
        notif_insert.assert_called_once()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _capture_rows() -> list[dict]:
    """Fake snapshot rows as they'd be returned by IntradayCloseSnapshotRepo."""
    return [
        {"trade_id": "TRD-1", "leg_order": 1, "symbol": "NIFTY",
         "expiry_date": _EXPIRY, "strike": 23200.0, "option_type": "CE",
         "ltp": 50.0, "source": "LIVE", "provider": "zerodha",
         "snapshot_date": _TODAY},
        {"trade_id": "TRD-1", "leg_order": 2, "symbol": "NIFTY",
         "expiry_date": _EXPIRY, "strike": 23300.0, "option_type": "CE",
         "ltp": 22.0, "source": "LIVE", "provider": "zerodha",
         "snapshot_date": _TODAY},
    ]


# ---------------------------------------------------------------------------
# Repo round-trip — IntradayCloseSnapshotRepo.insert_many idempotence
# ---------------------------------------------------------------------------
class TestIntradayCloseSnapshotRepo:
    def test_insert_many_empty_is_noop(self, mock_db):
        from database.models import IntradayCloseSnapshotRepo
        n = IntradayCloseSnapshotRepo(mock_db).insert_many([])
        assert n == 0
        mock_db.execute.assert_not_called()

    def test_insert_many_deletes_then_inserts(self, mock_db):
        """Each (date,trade_id,leg_order) key should generate a DELETE then
        an INSERT — idempotent re-run replaces today's capture."""
        from database.models import IntradayCloseSnapshotRepo
        rows = _capture_rows()
        for r in rows:
            r["captured_at"] = datetime(2026, 5, 4, 15, 35)
        IntradayCloseSnapshotRepo(mock_db).insert_many(rows)
        sqls = [c.args[0] for c in mock_db.execute.call_args_list]
        # 2 deletes + 2 inserts = 4 statements
        assert sum("DELETE FROM options_intraday_close_snapshot" in s for s in sqls) == 2
        assert sum("INSERT INTO options_intraday_close_snapshot" in s for s in sqls) == 2
