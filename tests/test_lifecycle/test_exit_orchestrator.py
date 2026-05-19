"""Tests for lifecycle/exit_orchestrator.py — daily exit decisions for open trades."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

import lifecycle.exit_orchestrator as orch


def _open_trade(trade_id: str = "TRD-1", suggestion_id: str = "SUG-1") -> dict:
    return {
        "trade_id": trade_id,
        "suggestion_id": suggestion_id,
        "trade_name": "TEST",
        "executed_on": date(2026, 4, 28),
        "position_type": "FULL",
        "net_credit_actual": 2250.0,
        "actual_max_profit": 2250.0,
        "actual_max_loss": 5250.0,
        "actual_stop_loss_level": None,
        "total_charges": 60.0,
        "status": "ACTIVE",
    }


def _sug_legs() -> list[dict]:
    """Bear call spread: short 23200 CE, long 23300 CE."""
    return [
        {"leg_order": 1, "symbol": "NIFTY", "expiry_date": date(2026, 5, 14),
         "strike": 23200.0, "option_type": "CE", "action": "SELL",
         "lots": 1, "lot_size": 75},
        {"leg_order": 2, "symbol": "NIFTY", "expiry_date": date(2026, 5, 14),
         "strike": 23300.0, "option_type": "CE", "action": "BUY",
         "lots": 1, "lot_size": 75},
    ]


def _trade_legs(filled: bool = True) -> list[dict]:
    return [
        {"leg_order": 1, "executed": 1 if filled else 0, "fill_price": 50.0},
        {"leg_order": 2, "executed": 1 if filled else 0, "fill_price": 20.0},
    ]


def _chain() -> list[dict]:
    return [
        {"strike": 23200.0, "option_type": "CE", "settle_price": 30.0, "close_price": 30.0},
        {"strike": 23300.0, "option_type": "CE", "settle_price": 12.0, "close_price": 12.0},
    ]


class TestRunExitEngine:
    def test_no_open_trades_returns_zero(self, mock_db, mocker):
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[])
        n = orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert n == 0

    def test_skips_trade_with_no_chain_data(self, mock_db, mocker):
        """No chain means market closed — must skip, not fire spurious decisions."""
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mocker.patch.object(orch.TradeRepo, "legs", return_value=_trade_legs())
        mock_db.fetch_all.return_value = _sug_legs()
        mock_db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        # Chain query → empty
        mocker.patch.object(orch.FoEodRepo, "get_chain", return_value=[])
        n = orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert n == 0  # skipped, no decisions made

    def test_holds_when_engine_returns_hold(self, mock_db, mocker):
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mocker.patch.object(orch.TradeRepo, "legs", return_value=_trade_legs())
        mock_db.fetch_all.return_value = _sug_legs()
        mock_db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        mocker.patch.object(orch.FoEodRepo, "get_chain", return_value=_chain())
        update_status = mocker.patch.object(orch.TradeRepo, "update_status")
        # evaluate_exit returns HOLD
        mocker.patch("lifecycle.exit_orchestrator.evaluate_exit",
                     return_value=MagicMock(decision="HOLD", reason="ok"))
        n = orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert n == 1
        # status set to ACTIVE/OPEN
        call_args = update_status.call_args
        assert call_args[0][1] == "ACTIVE"
        assert call_args[0][2] == "OPEN"

    def test_skips_legs_that_were_not_filled(self, mock_db, mocker):
        """If all legs are unfilled, evaluate_exit must not be called."""
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mocker.patch.object(orch.TradeRepo, "legs", return_value=_trade_legs(filled=False))
        mock_db.fetch_all.return_value = _sug_legs()
        mock_db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        mocker.patch.object(orch.FoEodRepo, "get_chain", return_value=_chain())
        eval_mock = mocker.patch("lifecycle.exit_orchestrator.evaluate_exit")
        n = orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert n == 0
        eval_mock.assert_not_called()


class TestSanitizedClosePrice:
    """Fix E — guard against bogus EOD chain rows (settle_price ≈ spot)."""

    def test_normal_mid_passes_through(self):
        px, src = orch._sanitized_close_price(
            option_type="CE", strike=24400.0, raw_mid=120.5, spot=24500.0,
        )
        assert px == 120.5
        assert src == "mid"

    def test_corrupt_mid_replaced_by_intrinsic_for_pe(self):
        # The exact production bug: NIFTY 24400 PE settle came back as ~23,618
        # (the spot value). Intrinsic = 24400 - 23618 = 782.
        px, src = orch._sanitized_close_price(
            option_type="PE", strike=24400.0, raw_mid=23618.0, spot=23618.0,
        )
        assert src == "intrinsic_fallback"
        assert px == pytest.approx(782.0)

    def test_corrupt_mid_replaced_by_intrinsic_for_ce(self):
        # CE upper cap is max(strike, spot) * 0.5 = 12,200; raw_mid 13,000 exceeds it.
        px, src = orch._sanitized_close_price(
            option_type="CE", strike=24400.0, raw_mid=13000.0, spot=20000.0,
        )
        assert src == "intrinsic_fallback"
        assert px == 0.0  # OTM CE (24400 strike, 20000 spot)

    def test_normal_deep_itm_passes_through(self):
        """A genuinely deep-ITM premium (≈ intrinsic + small time value)
        must NOT be flagged as bogus."""
        # PE 30000 with spot 22000 → intrinsic = 8000, premium ≈ 8200
        px, src = orch._sanitized_close_price(
            option_type="PE", strike=30000.0, raw_mid=8200.0, spot=22000.0,
        )
        assert px == 8200.0
        assert src == "mid"

    def test_negative_mid_replaced(self):
        px, src = orch._sanitized_close_price(
            option_type="PE", strike=24000.0, raw_mid=-5.0, spot=23500.0,
        )
        assert src == "intrinsic_fallback"
        assert px == pytest.approx(500.0)

    def test_no_spot_passes_through(self):
        # If we can't look up spot, leave the value alone (no fallback to compute).
        px, src = orch._sanitized_close_price(
            option_type="CE", strike=24400.0, raw_mid=120.5, spot=None,
        )
        assert px == 120.5
        assert src == "mid"


class TestAutoSettleAtExpire:
    """Fix B — when evaluate_exit returns EXPIRE we cash-settle automatically."""

    @staticmethod
    def _expiry_chain() -> list[dict]:
        """On expiry day NSE settles OTM contracts to zero. Use that as the
        synthetic 'close' chain so we exercise the full intrinsic-settlement path."""
        return [
            {"strike": 23200.0, "option_type": "CE",
             "settle_price": 0.0, "close_price": 0.0},
            {"strike": 23300.0, "option_type": "CE",
             "settle_price": 0.0, "close_price": 0.0},
        ]

    @staticmethod
    def _expire_setup(mocker, mock_db):
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mocker.patch.object(orch.TradeRepo, "legs", return_value=_trade_legs())
        mock_db.fetch_all.return_value = _sug_legs()
        mock_db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        mocker.patch.object(orch.FoEodRepo, "get_chain",
                            return_value=TestAutoSettleAtExpire._expiry_chain())
        # Spot well below the short 23200 → both CEs settle worthless.
        mocker.patch.object(orch.SpotEodRepo, "for_date",
                            return_value={"close_price": 22950.0})
        mocker.patch("lifecycle.exit_orchestrator.evaluate_exit",
                     return_value=MagicMock(decision="EXPIRE",
                                            reason="DTE=0 — settle today"))

    def test_expire_calls_close_trade(self, mock_db, mocker):
        """EXPIRE decision should auto-close the trade (status='CLOSED')."""
        self._expire_setup(mocker, mock_db)
        close = mocker.patch.object(orch.TradeRepo, "close_trade")
        update_leg = mocker.patch.object(orch.TradeRepo, "update_leg_exit")
        mocker.patch.object(orch.TradeRepo, "update_status")
        n = orch.run_exit_engine(mock_db, date(2026, 5, 14))
        assert n == 1
        close.assert_called_once()
        # Both legs settle worthless; short collected ₹50 fill, long paid ₹20 fill.
        # gross = (50 - 0) * 75 - (0 - 20) * 75? Wait sign:
        #   short CE: (fill - close) * qty = (50 - 0) * 75 = +3750
        #   long  CE: (close - fill) * qty = (0 - 20) * 75 = -1500
        #   net gross ₹2,250 (= net_credit_actual, makes sense at expiry)
        kwargs = close.call_args.kwargs
        assert kwargs["gross"] == pytest.approx(2250.0)
        assert kwargs["charges"] == pytest.approx(60.0)
        assert kwargs["net"] == pytest.approx(2190.0)
        # update_leg_exit must be called twice (one per filled leg).
        assert update_leg.call_count == 2

    def test_expire_emits_critical_notification(self, mock_db, mocker):
        self._expire_setup(mocker, mock_db)
        mocker.patch.object(orch.TradeRepo, "close_trade")
        mocker.patch.object(orch.TradeRepo, "update_leg_exit")
        mocker.patch.object(orch.TradeRepo, "update_status")
        insert = mocker.patch.object(orch.NotificationRepo, "insert")
        orch.run_exit_engine(mock_db, date(2026, 5, 14))
        kinds = [c.args[0].notif_type for c in insert.call_args_list]
        sevs = [c.args[0].severity for c in insert.call_args_list]
        assert "AUTO_SETTLED" in kinds
        idx = kinds.index("AUTO_SETTLED")
        assert sevs[idx] == "CRITICAL"


class TestExitNotificationSeverity:
    """Fix A — exit alerts must be CRITICAL/WARNING, never silent INFO."""

    @pytest.mark.parametrize("decision,expected_sev", [
        ("TAKE_PROFIT",       "CRITICAL"),
        ("SL_HIT",            "CRITICAL"),
        ("EXIT_TOMORROW",     "WARNING"),
        ("TIME_DECAY_DONE",   "WARNING"),
    ])
    def test_severity_mapping(self, mock_db, mocker, decision, expected_sev):
        mocker.patch.object(orch.TradeRepo, "open_trades", return_value=[_open_trade()])
        mocker.patch.object(orch.TradeRepo, "legs", return_value=_trade_legs())
        mock_db.fetch_all.return_value = _sug_legs()
        mock_db.fetch_one.return_value = {"strategy": "BEAR_CALL_SPREAD"}
        mocker.patch.object(orch.FoEodRepo, "get_chain", return_value=_chain())
        mocker.patch.object(orch.SpotEodRepo, "for_date",
                            return_value={"close_price": 23000.0})
        mocker.patch("lifecycle.exit_orchestrator.evaluate_exit",
                     return_value=MagicMock(decision=decision, reason="x"))
        mocker.patch.object(orch.TradeRepo, "update_status")
        insert = mocker.patch.object(orch.NotificationRepo, "insert")
        orch.run_exit_engine(mock_db, date(2026, 5, 4))
        assert insert.call_count >= 1
        note = insert.call_args.args[0]
        assert note.severity == expected_sev
        assert note.notif_type == f"EXIT_{decision}"
