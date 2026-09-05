"""Tests for lifecycle/trade_executor.py — mark_executed + supplement + close."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from contracts import TradeLegFill
from lifecycle import trade_executor as te


@pytest.fixture
def fake_suggestion():
    return {
        "trade_name": "N-IC-1",
        "strategy": "IRON_CONDOR",
        "underlying": "NIFTY",
        "expiry_date": "2026-05-14",
        "max_profit": 6000.0, "max_loss": 14000.0,
        "upper_breakeven": 23300.0, "lower_breakeven": 22700.0,
        "stop_loss_level": 23250.0,
        # Required by engine.execution_validator pre-execution gate
        "status": "PENDING",
        "spot_at_generation": 23000.0,
        "validator_status": None,
        "data_as_of": None,
        "entry_date": None,
    }


@pytest.fixture
def fake_legs():
    # Strikes clear engine.execution_validator's 1.5% buffer vs spot 23000.
    return [
        {"id": 1, "leg_order": 1, "symbol": "NIFTY",
         "expiry_date": "2026-05-14",
         "strike": 23500.0, "option_type": "CE", "action": "SELL",
         "lots": 1, "lot_size": 75},
        {"id": 2, "leg_order": 2, "symbol": "NIFTY",
         "expiry_date": "2026-05-14",
         "strike": 23600.0, "option_type": "CE", "action": "BUY",
         "lots": 1, "lot_size": 75},
        {"id": 3, "leg_order": 3, "symbol": "NIFTY",
         "expiry_date": "2026-05-14",
         "strike": 22500.0, "option_type": "PE", "action": "SELL",
         "lots": 1, "lot_size": 75},
        {"id": 4, "leg_order": 4, "symbol": "NIFTY",
         "expiry_date": "2026-05-14",
         "strike": 22400.0, "option_type": "PE", "action": "BUY",
         "lots": 1, "lot_size": 75},
    ]


# ---------------------------------------------------------------------------
class TestMarkExecuted:
    def test_raises_when_suggestion_unknown(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=None)
        with pytest.raises(ValueError, match="Unknown"):
            te.mark_executed(mock_db, "SUG-X", [])

    def test_raises_when_no_legs(self, mock_db, mocker, fake_suggestion):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=[])
        with pytest.raises(ValueError, match="no legs"):
            te.mark_executed(mock_db, "SUG-X", [])

    def test_void_when_no_fills(self, mock_db, mocker, fake_suggestion, fake_legs):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        update_status = mocker.patch(
            "lifecycle.trade_executor.SuggestionRepo.update_status")
        result = te.mark_executed(mock_db, "SUG-X", [])
        assert result is None
        update_status.assert_called_with("SUG-X", "IGNORED")
        mock_db.commit.assert_called()

    def test_full_valid_when_all_filled(self, mock_db, mocker, fake_suggestion, fake_legs):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-001")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        fills = [TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                              fill_time=datetime(2026, 5, 4, 9, 30))
                 for i in (1, 2, 3, 4)]
        tid = te.mark_executed(mock_db, "SUG-X", fills, spot_at_execution=23000.0)
        assert tid == "TRD-001"
        # The position_type should be FULL_VALID since all legs filled
        call_arg = ins.call_args[0][0]
        assert call_arg["position_type"] == "FULL_VALID"
        assert call_arg["broken_state_json"] is None

    def test_partial_records_broken_options(self, mock_db, mocker,
                                             fake_suggestion, fake_legs):
        """Only short legs filled — diagnose returns NAKED_SHORT."""
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-002")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        # Only fills for short legs (1 + 3) — long legs not filled
        fills = [
            TradeLegFill(leg_order=1, executed=True, fill_price=60.0,
                         fill_time=datetime(2026, 5, 4, 9, 30)),
            TradeLegFill(leg_order=3, executed=True, fill_price=55.0,
                         fill_time=datetime(2026, 5, 4, 9, 30)),
        ]
        tid = te.mark_executed(mock_db, "SUG-X", fills)
        assert tid == "TRD-002"
        call_arg = ins.call_args[0][0]
        assert call_arg["position_type"] != "FULL_VALID"
        assert call_arg["broken_state_json"] is not None


# ---------------------------------------------------------------------------
class TestSupplementTrade:
    def test_raises_when_trade_unknown(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get", return_value=None)
        with pytest.raises(ValueError, match="Unknown trade"):
            te.supplement_trade(mock_db, "TRD-X", [])

    def test_applies_new_fills_and_recomputes(self, mock_db, mocker, fake_legs):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        # First call: existing legs (executed=True for shorts only)
        # Second call: recomputed (after update)
        legs_with_state = [
            {**l, "executed": (l["leg_order"] in (1, 3)),
             "fill_price": 50.0 if l["leg_order"] in (1, 3) else None,
             "lots_actual": l["lots"] if l["leg_order"] in (1, 3) else None}
            for l in fake_legs
        ]
        # After supplementing leg 2:
        legs_after = [
            {**l, "executed": (l["leg_order"] in (1, 2, 3)),
             "fill_price": 50.0 if l["leg_order"] in (1, 2, 3) else None,
             "lots_actual": l["lots"] if l["leg_order"] in (1, 2, 3) else None}
            for l in fake_legs
        ]
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     side_effect=[legs_with_state, legs_after])
        mocker.patch("lifecycle.trade_executor.TradeRepo.update_leg_fill")
        upd = mocker.patch("lifecycle.trade_executor.TradeRepo.update_position")
        new_fills = [TradeLegFill(leg_order=2, executed=True, fill_price=30.0,
                                   fill_time=datetime(2026, 5, 4, 10, 0))]
        te.supplement_trade(mock_db, "TRD-1", new_fills)
        upd.assert_called_once()
        mock_db.commit.assert_called()

    def test_does_not_overwrite_already_executed_leg(self, mock_db, mocker, fake_legs):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        legs_with_state = [
            {**l, "executed": (l["leg_order"] in (1, 3)),
             "fill_price": 50.0 if l["leg_order"] in (1, 3) else None,
             "lots_actual": l["lots"] if l["leg_order"] in (1, 3) else None}
            for l in fake_legs
        ]
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     return_value=legs_with_state)
        upd_fill = mocker.patch("lifecycle.trade_executor.TradeRepo.update_leg_fill")
        mocker.patch("lifecycle.trade_executor.TradeRepo.update_position")
        # Try to overwrite executed leg 1 and also fill pending leg 2
        fills = [
            TradeLegFill(leg_order=1, executed=True, fill_price=99.0,
                         fill_time=datetime(2026, 5, 4, 10, 0)),
            TradeLegFill(leg_order=2, executed=True, fill_price=30.0,
                         fill_time=datetime(2026, 5, 4, 10, 0)),
        ]
        te.supplement_trade(mock_db, "TRD-1", fills)
        updated_orders = [c.args[1] for c in upd_fill.call_args_list]
        assert 1 not in updated_orders
        assert 2 in updated_orders


# ---------------------------------------------------------------------------
class TestCloseTradeWithFills:
    def test_raises_when_trade_unknown(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get", return_value=None)
        with pytest.raises(ValueError, match="Unknown trade"):
            te.close_trade_with_fills(mock_db, "TRD-X", [])

    def test_raises_when_no_executed_legs(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     return_value=[{"executed": False, "leg_order": 1}])
        with pytest.raises(ValueError, match="no executed legs"):
            te.close_trade_with_fills(mock_db, "TRD-1", [])

    def test_computes_pnl_and_closes(self, mock_db, mocker, fake_legs):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1"})
        # All 4 legs executed at 50/25/50/25
        prices = {1: 50.0, 2: 25.0, 3: 50.0, 4: 25.0}
        legs_open = [
            {**l, "executed": True, "fill_price": prices[l["leg_order"]],
             "lots_actual": l["lots"]} for l in fake_legs
        ]
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     return_value=legs_open)
        upd_exit = mocker.patch("lifecycle.trade_executor.TradeRepo.update_leg_exit")
        close = mocker.patch("lifecycle.trade_executor.TradeRepo.close_trade")
        # Exit at half — SELL gain = (50−25)*75 each, BUY loss = (25−12.5)*75 each
        exits = [
            {"leg_order": 1, "exit_price": 25.0, "exit_time": datetime(2026, 5, 7, 15, 0)},
            {"leg_order": 2, "exit_price": 12.5, "exit_time": datetime(2026, 5, 7, 15, 0)},
            {"leg_order": 3, "exit_price": 25.0, "exit_time": datetime(2026, 5, 7, 15, 0)},
            {"leg_order": 4, "exit_price": 12.5, "exit_time": datetime(2026, 5, 7, 15, 0)},
        ]
        te.close_trade_with_fills(mock_db, "TRD-1", exits)
        # Each leg exit recorded
        assert upd_exit.call_count == 4
        # close_trade called with computed gross_pnl
        close.assert_called_once()
        gross = close.call_args[0][1]  # positional: trade_id, gross, charges, net
        # SELL: (50-25)*75 = 1875 × 2 = 3750
        # BUY:  -(25-12.5)*75 = -937.5 × 2 = -1875
        # gross = 3750 - 1875 = 1875
        assert gross == pytest.approx(1875.0)

    def test_rejects_already_closed_trade(self, mock_db, mocker):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1", "status": "CLOSED"})
        with pytest.raises(ValueError, match="cannot close again"):
            te.close_trade_with_fills(
                mock_db, "TRD-1",
                [{"leg_order": 1, "exit_price": 10.0, "exit_time": None}],
            )

    def test_requires_exit_for_every_executed_leg(self, mock_db, mocker, fake_legs):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1", "status": "ACTIVE"})
        legs_open = [
            {**l, "executed": True, "fill_price": 50.0, "lots_actual": l["lots"]}
            for l in fake_legs
        ]
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     return_value=legs_open)
        with pytest.raises(ValueError, match="every executed leg"):
            te.close_trade_with_fills(
                mock_db, "TRD-1",
                [{"leg_order": 1, "exit_price": 25.0, "exit_time": None}],
            )

    @pytest.mark.parametrize("status", ["VOID", "EXPIRED"])
    def test_rejects_void_or_expired_trade(self, mock_db, mocker, status):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1", "status": status})
        with pytest.raises(ValueError, match="cannot close again"):
            te.close_trade_with_fills(
                mock_db, "TRD-1",
                [{"leg_order": 1, "exit_price": 10.0, "exit_time": None}],
            )

    def test_close_after_kite_flatten_is_not_blocked(self, mock_db, mocker, fake_legs):
        """Closing on Kite then recording exits here must still work.

        COMPLETE (or even leftover OPEN) broker rows are ignored — close never
        consults options_broker_orders.
        """
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1", "status": "ACTIVE"})
        prices = {1: 50.0, 2: 25.0, 3: 50.0, 4: 25.0}
        legs_open = [
            {**l, "executed": True, "fill_price": prices[l["leg_order"]],
             "lots_actual": l["lots"]} for l in fake_legs
        ]
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     return_value=legs_open)
        mocker.patch("lifecycle.trade_executor.TradeRepo.update_leg_exit")
        close = mocker.patch("lifecycle.trade_executor.TradeRepo.close_trade")
        mock_db.fetch_all.return_value = [
            {"status": "COMPLETE", "operation": "ENTRY", "trade_id": "TRD-1"},
            {"status": "COMPLETE", "operation": "EXIT", "trade_id": "TRD-1"},
        ]
        exits = [
            {"leg_order": i, "exit_price": 20.0, "exit_time": datetime(2026, 5, 7, 15, 0)}
            for i in (1, 2, 3, 4)
        ]
        te.close_trade_with_fills(mock_db, "TRD-1", exits)
        close.assert_called_once()
        mock_db.commit.assert_called()

    def test_close_not_blocked_when_kite_orders_still_open(
        self, mock_db, mocker, fake_legs,
    ):
        mocker.patch("lifecycle.trade_executor.TradeRepo.get",
                     return_value={"trade_id": "TRD-1", "status": "ACTIVE"})
        legs_open = [
            {**l, "executed": True, "fill_price": 50.0, "lots_actual": l["lots"]}
            for l in fake_legs
        ]
        mocker.patch("lifecycle.trade_executor.TradeRepo.legs_with_suggestion_info",
                     return_value=legs_open)
        mocker.patch("lifecycle.trade_executor.TradeRepo.update_leg_exit")
        close = mocker.patch("lifecycle.trade_executor.TradeRepo.close_trade")
        mock_db.fetch_all.return_value = [{"status": "OPEN", "operation": "EXIT"}]
        exits = [
            {"leg_order": i, "exit_price": 20.0, "exit_time": None}
            for i in (1, 2, 3, 4)
        ]
        te.close_trade_with_fills(mock_db, "TRD-1", exits)
        close.assert_called_once()


# ---------------------------------------------------------------------------
# C2 — additional trade executor tests for new behaviours
# ---------------------------------------------------------------------------

class TestCircuitBreakerBlocks:
    """Circuit breaker active → execution blocked (ValueError)."""

    def test_ignore_bypasses_circuit_breaker(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=True,
        )
        update_status = mocker.patch(
            "lifecycle.trade_executor.SuggestionRepo.update_status")
        result = te.mark_executed(mock_db, "SUG-X", [])
        assert result is None
        update_status.assert_called_with("SUG-X", "IGNORED")

    def test_blocked_when_circuit_breaker_active(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        # Simulate circuit breaker ON
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=True,
        )
        fills = [TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                              fill_time=datetime(2026, 5, 4, 9, 30))
                 for i in (1, 2, 3, 4)]
        with pytest.raises(ValueError, match="Execution blocked"):
            te.mark_executed(mock_db, "SUG-X", fills)
        with pytest.raises(ValueError, match="Execution blocked"):
            te.mark_executed(
                mock_db, "SUG-X", [], execute_at_suggested=True,
            )

    def test_fail_closed_when_flag_unreadable(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            side_effect=RuntimeError("db down"),
        )
        fills = [TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                              fill_time=datetime(2026, 5, 4, 9, 30))
                 for i in (1, 2, 3, 4)]
        with pytest.raises(ValueError, match="circuit-breaker flag could not be read"):
            te.mark_executed(mock_db, "SUG-X", fills)

    def test_execute_at_suggested_skips_stale_gate(
        self, mock_db, mocker, fake_legs,
    ):
        stale_sug = {
            "trade_name": "N-IC-1",
            "max_profit": 6000.0, "max_loss": 14000.0,
            "upper_breakeven": 23300.0, "lower_breakeven": 22700.0,
            "stop_loss_level": 23250.0,
            "status": "PENDING",
            "spot_at_generation": 23000.0,
            "validator_status": None,
            "data_as_of": datetime(2026, 5, 4, 8, 0),
            "entry_date": None,
            "data_source": "LIVE",
            "trigger_type": "LIVE_RUN",
            "generated_on": datetime(2026, 5, 4, 8, 0),
        }
        legs = [
            {**leg, "suggested_price": 50.0 + i}
            for i, leg in enumerate(fake_legs, start=0)
        ]
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=stale_sug)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-STALE")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        prov = mocker.patch(
            "lifecycle.trade_executor.TradeRepo.write_execution_provenance",
        )
        tid = te.mark_executed(
            mock_db, "SUG-X", [], execute_at_suggested=True,
        )
        assert tid == "TRD-STALE"
        call_arg = ins.call_args[0][0]
        assert call_arg["position_type"] == "FULL_VALID"
        assert call_arg["spot_at_execution"] == 23000.0
        prov.assert_called_once()
        assert prov.call_args.kwargs.get("gate_passed") is False

    def test_execute_at_suggested_skips_strategy_veto_gate(
        self, mock_db, mocker, fake_legs,
    ):
        import json as _json
        vetoed = {
            "trade_name": "BN-STRADDLE-1",
            "max_profit": None, "max_loss": 28000.0,
            "upper_breakeven": 55800.0, "lower_breakeven": 54200.0,
            "stop_loss_level": None,
            "status": "PENDING",
            "spot_at_generation": 55000.0,
            "validator_status": None,
            "data_as_of": datetime(2026, 8, 18, 9, 30),
            "entry_date": date(2026, 8, 18),
            "data_source": "LIVE",
            "trigger_type": "LIVE_RUN",
            "generated_on": datetime(2026, 8, 18, 9, 30),
            "trigger_reason": _json.dumps({
                "regime_pair_group": "BANKNIFTY:Weekly:2026-08-19",
                "regime_pair_type": "breakout",
                "regime_pair_preferred": False,
                "strategy_veto": "LONG_STRADDLE vetoed: IV rank 5 below 15",
            }),
        }
        legs = [
            {**leg, "suggested_price": 50.0 + i, "action": "BUY"}
            for i, leg in enumerate(fake_legs[:2], start=0)
        ]
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=vetoed)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-VETO")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        mocker.patch(
            "lifecycle.trade_executor.TradeRepo.write_execution_provenance",
        )
        tid = te.mark_executed(
            mock_db, "SUG-X", [], execute_at_suggested=True,
        )
        assert tid == "TRD-VETO"
        assert ins.call_args[0][0]["position_type"] == "FULL_VALID"

    def test_live_execute_blocked_by_strategy_veto(
        self, mock_db, mocker, fake_legs,
    ):
        import json as _json
        vetoed = {
            "trade_name": "BN-STRADDLE-1",
            "max_profit": None, "max_loss": 28000.0,
            "upper_breakeven": 55800.0, "lower_breakeven": 54200.0,
            "stop_loss_level": None,
            "status": "PENDING",
            "spot_at_generation": 55000.0,
            "validator_status": None,
            "data_as_of": datetime(2026, 8, 18, 9, 30),
            "entry_date": None,
            "data_source": "EOD",
            "trigger_type": "EOD_RUN",
            "generated_on": datetime(2026, 8, 18, 9, 30),
            "trigger_reason": _json.dumps({
                "regime_pair_group": "BANKNIFTY:Weekly:2026-08-19",
                "regime_pair_type": "breakout",
                "regime_pair_preferred": False,
                "strategy_veto": "LONG_STRADDLE vetoed: IV rank 5 below 15",
            }),
        }
        legs = [
            {**leg, "suggested_price": 50.0, "action": "BUY"}
            for leg in fake_legs[:2]
        ]
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=vetoed)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=legs)
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        fills = [TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                              fill_time=datetime(2026, 8, 18, 9, 40))
                 for i in (1, 2)]
        with pytest.raises(ValueError, match="IV rank"):
            te.mark_executed(mock_db, "SUG-X", fills)

    def test_skip_gate_records_manual_fill_prices(
        self, mock_db, mocker, fake_legs,
    ):
        stale_sug = {
            "trade_name": "N-IC-1",
            "max_profit": 6000.0, "max_loss": 14000.0,
            "upper_breakeven": 23300.0, "lower_breakeven": 22700.0,
            "stop_loss_level": 23250.0,
            "status": "PENDING",
            "spot_at_generation": 23000.0,
            "validator_status": None,
            "data_as_of": datetime(2026, 5, 4, 8, 0),
            "entry_date": None,
            "data_source": "LIVE",
            "trigger_type": "LIVE_RUN",
            "generated_on": datetime(2026, 5, 4, 8, 0),
        }
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=stale_sug)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-MANUAL")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        mocker.patch(
            "lifecycle.trade_executor.TradeRepo.write_execution_provenance",
        )
        fills = [
            TradeLegFill(leg_order=i, executed=True, fill_price=12.5 * i, fill_time=None)
            for i in range(1, 5)
        ]
        tid = te.mark_executed(
            mock_db, "SUG-X", fills, skip_execution_gate=True,
        )
        assert tid == "TRD-MANUAL"
        call_arg = ins.call_args[0][0]
        assert call_arg["net_credit_actual"] != 0
        assert all(f.fill_time is not None for f in fills)


class TestActualNetCreditComputation:
    """net_credit_actual must reflect actual fill prices, not suggestion prices."""

    def test_actual_net_credit_uses_fill_price(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-003")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        # legs 1 + 3 are SELL at 60/55; legs 2 + 4 are BUY at 30/25
        fill_prices = {1: 60.0, 2: 30.0, 3: 55.0, 4: 25.0}
        fills = [TradeLegFill(leg_order=i, executed=True,
                              fill_price=fill_prices[i],
                              fill_time=datetime(2026, 5, 4, 9, 30))
                 for i in (1, 2, 3, 4)]
        te.mark_executed(mock_db, "SUG-X", fills)
        call_arg = ins.call_args[0][0]
        # SELL = +1, BUY = -1
        # net = (60 + 55 - 30 - 25) × 75 = 60 × 75 = 4500
        assert call_arg["net_credit_actual"] == pytest.approx(4500.0)
        # Credit trade: max profit = actual credit; max loss = suggested width − credit
        assert call_arg["actual_max_profit"] == pytest.approx(4500.0)
        assert call_arg["actual_max_loss"] == pytest.approx(15500.0)
        # Fill BEs: net credit/share 60 → short call 23500+60, short put 22500-60
        assert call_arg["actual_upper_breakeven"] == pytest.approx(23560.0)
        assert call_arg["actual_lower_breakeven"] == pytest.approx(22440.0)

    def test_blocks_manual_fill_when_zerodha_orders_in_flight(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mock_db.fetch_all.return_value = [{"id": 1, "status": "OPEN"}]
        fills = [TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                              fill_time=datetime(2026, 5, 4, 9, 30))
                 for i in (1, 2, 3, 4)]
        with pytest.raises(ValueError, match="already in flight"):
            te.mark_executed(mock_db, "SUG-X", fills)

    def test_blocks_manual_fill_when_orphan_entry_fills_exist(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch(
            "database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion",
            return_value=[],
        )
        mocker.patch(
            "database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills",
            return_value=[{"id": 1, "status": "COMPLETE", "trade_id": None}],
        )
        fills = [TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                              fill_time=datetime(2026, 5, 4, 9, 30))
                 for i in (1, 2, 3, 4)]
        with pytest.raises(ValueError, match="without a recorded trade"):
            te.mark_executed(mock_db, "SUG-X", fills)

    def test_completed_booked_kite_orders_do_not_block_manual_fill(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        """COMPLETE rows with a trade_id are not in-flight and not orphans."""
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-KITE")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        mocker.patch(
            "database.broker_order_repo.BrokerOrderRepo.pending_for_suggestion",
            return_value=[],
        )
        mocker.patch(
            "database.broker_order_repo.BrokerOrderRepo.orphan_entry_fills",
            return_value=[],
        )
        fills = [TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                              fill_time=datetime(2026, 5, 4, 9, 30))
                 for i in (1, 2, 3, 4)]
        tid = te.mark_executed(mock_db, "SUG-X", fills)
        assert tid == "TRD-KITE"
        assert ins.called

    def test_ignore_not_blocked_by_open_kite_orders(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        update_status = mocker.patch(
            "lifecycle.trade_executor.SuggestionRepo.update_status")
        mock_db.fetch_all.return_value = [{"status": "OPEN"}]
        result = te.mark_executed(mock_db, "SUG-X", [])
        assert result is None
        update_status.assert_called_with("SUG-X", "IGNORED")


class TestActualMaxEconomics:
    def test_credit_uses_actual_credit(self):
        sug = {"max_profit": 6000.0, "max_loss": 14000.0}
        assert te._actual_max_profit(sug, 4500.0) == pytest.approx(4500.0)
        assert te._actual_max_loss(sug, 4500.0) == pytest.approx(15500.0)

    def test_debit_scales_from_suggested_width(self):
        sug = {"max_profit": 12000.0, "max_loss": 3000.0}
        assert te._actual_max_profit(sug, -4000.0) == pytest.approx(11000.0)
        assert te._actual_max_loss(sug, -4000.0) == pytest.approx(4000.0)

    def test_zero_credit_keeps_suggestion_values(self):
        sug = {"max_profit": 6000.0, "max_loss": 14000.0}
        assert te._actual_max_profit(sug, 0.0) == 6000.0
        assert te._actual_max_loss(sug, 0.0) == 14000.0

    def test_missing_suggestion_economics(self):
        assert te._actual_max_profit({}, 100.0) == pytest.approx(100.0)
        assert te._actual_max_loss({}, 100.0) is None
        assert te._actual_max_profit({"max_profit": 1.0}, -50.0) == 1.0

    def test_debit_width_scales_with_lots(self):
        sug = {"max_profit": 12000.0, "max_loss": 3000.0}
        assert te._actual_max_profit(sug, -8000.0, width_scale=2.0) == pytest.approx(22000.0)
        assert te._actual_max_loss(sug, -8000.0, width_scale=2.0) == pytest.approx(8000.0)

    def test_credit_width_scales_with_lots(self):
        sug = {"max_profit": 6000.0, "max_loss": 14000.0}
        assert te._actual_max_profit(sug, 9000.0, width_scale=2.0) == pytest.approx(9000.0)
        assert te._actual_max_loss(sug, 9000.0, width_scale=2.0) == pytest.approx(31000.0)


class TestLotsOverride:
    """TradeLegFill.lots_override must override default lots when provided."""

    def test_lots_override_affects_net_credit(
        self, mock_db, mocker, fake_suggestion, fake_legs
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-004")
        ins = mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        # Override to 2 lots for SELL legs, 1 lot for BUY legs
        fills = [
            TradeLegFill(leg_order=1, executed=True, fill_price=60.0,
                         fill_time=datetime(2026, 5, 4, 9, 30), lots_override=2),
            TradeLegFill(leg_order=2, executed=True, fill_price=30.0,
                         fill_time=datetime(2026, 5, 4, 9, 30), lots_override=1),
            TradeLegFill(leg_order=3, executed=True, fill_price=55.0,
                         fill_time=datetime(2026, 5, 4, 9, 30), lots_override=2),
            TradeLegFill(leg_order=4, executed=True, fill_price=25.0,
                         fill_time=datetime(2026, 5, 4, 9, 30), lots_override=1),
        ]
        te.mark_executed(mock_db, "SUG-X", fills)
        call_arg = ins.call_args[0][0]
        # SELL: (60*2 + 55*2)*75 = 175×75 = 13125
        # BUY:  -(30*1 + 25*1)*75 = -55×75 = -4125
        expected = (60 * 2 + 55 * 2 - 30 * 1 - 25 * 1) * 75
        assert call_arg["net_credit_actual"] == pytest.approx(float(expected))


class TestExecutionFlagsAndLotSize:
    def test_execute_at_suggested_records_gate_passed_when_gate_ok(
        self, mock_db, mocker, fake_suggestion, fake_legs,
    ):
        legs = [{**leg, "suggested_price": 50.0} for leg in fake_legs]
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-GATE-OK")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        prov = mocker.patch(
            "lifecycle.trade_executor.TradeRepo.write_execution_provenance",
        )
        tid = te.mark_executed(
            mock_db, "SUG-X", [], execute_at_suggested=True,
        )
        assert tid == "TRD-GATE-OK"
        assert prov.call_args.kwargs.get("gate_passed") is True

    def test_skip_flag_ignored_when_gate_ok(
        self, mock_db, mocker, fake_suggestion, fake_legs,
    ):
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=fake_legs)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.update_status")
        mocker.patch("lifecycle.trade_executor.TradeRepo.next_trade_id",
                     return_value="TRD-SKIP-OK")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert")
        mocker.patch("lifecycle.trade_executor.TradeRepo.insert_legs")
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        prov = mocker.patch(
            "lifecycle.trade_executor.TradeRepo.write_execution_provenance",
        )
        fills = [
            TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                         fill_time=datetime(2026, 5, 4, 9, 30))
            for i in (1, 2, 3, 4)
        ]
        tid = te.mark_executed(
            mock_db, "SUG-X", fills, skip_execution_gate=True,
        )
        assert tid == "TRD-SKIP-OK"
        assert prov.call_args.kwargs.get("gate_passed") is True

    def test_rejects_zero_lot_size(
        self, mock_db, mocker, fake_suggestion, fake_legs,
    ):
        legs = [{**leg, "lot_size": 0} for leg in fake_legs]
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.get",
                     return_value=fake_suggestion)
        mocker.patch("lifecycle.trade_executor.SuggestionRepo.legs",
                     return_value=legs)
        mocker.patch(
            "lifecycle.trade_executor.RuntimeFlagsRepo.get_bool",
            return_value=False,
        )
        fills = [
            TradeLegFill(leg_order=i, executed=True, fill_price=50.0,
                         fill_time=datetime(2026, 5, 4, 9, 30))
            for i in (1, 2, 3, 4)
        ]
        with pytest.raises(ValueError, match="lot_size"):
            te.mark_executed(mock_db, "SUG-X", fills)
