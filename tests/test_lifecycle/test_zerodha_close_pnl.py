"""Tests for lifecycle/zerodha_close_pnl.py — per-trade Kite P&L overwrite."""

from datetime import date
from unittest.mock import MagicMock

from lifecycle.zerodha_close_pnl import (
    closed_zerodha_trade_ids_on,
    reconcile_closed_trade_from_kite,
    reconcile_closed_zerodha_trades_on,
)


def _broker_row(*, oid, trade_id, leg, op, symbol, qty=65, status="COMPLETE"):
    return {
        "kite_order_id": oid,
        "trade_id": trade_id,
        "leg_order": leg,
        "operation": op,
        "status": status,
        "tradingsymbol": symbol,
        "exchange": "NFO",
        "quantity": qty,
        "filled_quantity": qty,
        "transaction_type": "BUY" if op == "ENTRY" else "SELL",
    }


def _complete_history(oid, symbol, txn, avg, qty=65):
    return [{
        "order_id": oid,
        "status": "COMPLETE",
        "tradingsymbol": symbol,
        "exchange": "NFO",
        "transaction_type": txn,
        "variety": "regular",
        "product": "NRML",
        "order_type": "LIMIT",
        "average_price": avg,
        "filled_quantity": qty,
        "quantity": qty,
    }]


def _note_row(symbol, txn, total):
    return {
        "tradingsymbol": symbol,
        "transaction_type": txn,
        "charges": {"total": total},
    }


def _patch_repos(mocker, *, has_entry, broker_rows, legs, other_by_oid=None):
    broker = MagicMock()
    broker.has_entry_kite_fills_for_trade.return_value = has_entry
    broker.by_trade.return_value = broker_rows

    def _by_oid(oid):
        mapping = other_by_oid or {}
        return mapping.get(oid, [r for r in broker_rows if r.get("kite_order_id") == oid])

    broker.by_kite_order_id.side_effect = _by_oid
    mocker.patch(
        "lifecycle.zerodha_close_pnl.BrokerOrderRepo",
        return_value=broker,
    )
    trd = MagicMock()
    trd.get.return_value = {"trade_id": "TRD-A", "status": "CLOSED"}
    trd.legs_with_suggestion_info.return_value = legs
    mocker.patch("lifecycle.zerodha_close_pnl.TradeRepo", return_value=trd)
    return broker, trd


def test_skips_paper_trade_without_opening_kite_fills(mocker):
    broker, trd = _patch_repos(
        mocker, has_entry=False, broker_rows=[], legs=[],
    )
    facade = MagicMock()
    db = MagicMock()
    out = reconcile_closed_trade_from_kite(db, "TRD-PAPER", facade)
    assert out is None
    facade.order_history.assert_not_called()
    facade.get_virtual_contract_note.assert_not_called()
    trd.update_pnl.assert_not_called()
    broker.by_trade.assert_not_called()


def test_applies_this_trade_orders_only(mocker):
    sym = "NIFTY26MAY23000CE"
    rows = [
        _broker_row(oid="OID-ENTRY-A", trade_id="TRD-A", leg=1, op="ENTRY", symbol=sym),
        _broker_row(oid="OID-EXIT-A", trade_id="TRD-A", leg=1, op="EXIT", symbol=sym),
        # A row stamped with another trade must be ignored even if it leaked into the list.
        _broker_row(oid="OID-B", trade_id="TRD-B", leg=1, op="ENTRY", symbol=sym),
    ]
    legs = [{
        "leg_order": 1, "executed": True, "action": "BUY",
        "fill_price": 100.0, "exit_price": 90.0,
        "lots": 1, "lots_actual": 1, "lot_size": 65,
        "fill_time": None, "exit_time": None,
    }]
    broker, trd = _patch_repos(mocker, has_entry=True, broker_rows=rows, legs=legs)
    facade = MagicMock()

    def _hist(oid):
        if oid == "OID-ENTRY-A":
            return _complete_history(oid, sym, "BUY", 100.0)
        if oid == "OID-EXIT-A":
            return _complete_history(oid, sym, "SELL", 90.0)
        raise AssertionError(f"fetched another trade's order {oid}")

    facade.order_history.side_effect = _hist
    facade.get_virtual_contract_note.return_value = [
        _note_row(sym, "BUY", 23.5),
        _note_row(sym, "SELL", 26.5),
    ]
    db = MagicMock()
    out = reconcile_closed_trade_from_kite(db, "TRD-A", facade)

    assert out is not None
    assert out["trade_id"] == "TRD-A"
    assert out["kite_order_ids"] == ["OID-ENTRY-A", "OID-EXIT-A"]
    # BUY: (90-100)*65 = -650
    assert out["gross_pnl"] == -650.0
    assert out["total_charges"] == 50.0
    assert out["net_pnl"] == -700.0
    hist_ids = [c.args[0] for c in facade.order_history.call_args_list]
    assert hist_ids == ["OID-ENTRY-A", "OID-EXIT-A"]
    note_params = facade.get_virtual_contract_note.call_args.args[0]
    assert [p["order_id"] for p in note_params] == ["OID-ENTRY-A", "OID-EXIT-A"]
    trd.update_pnl.assert_called_once_with("TRD-A", -650.0, 50.0, -700.0)
    db.commit.assert_called()


def test_uses_extra_exit_ids_owned_by_this_close(mocker):
    """Already-flat close: EXIT kite id is not yet on broker rows for this trade."""
    sym = "NIFTY26MAY23000CE"
    rows = [
        _broker_row(oid="OID-ENTRY-A", trade_id="TRD-A", leg=1, op="ENTRY", symbol=sym),
    ]
    legs = [{
        "leg_order": 1, "executed": True, "action": "BUY",
        "fill_price": 100.0, "exit_price": 88.0,
        "lots": 1, "lots_actual": 1, "lot_size": 65,
        "fill_time": None, "exit_time": None,
    }]
    _, trd = _patch_repos(mocker, has_entry=True, broker_rows=rows, legs=legs)
    facade = MagicMock()

    def _hist(oid):
        if oid == "OID-ENTRY-A":
            return _complete_history(oid, sym, "BUY", 100.0)
        if oid == "OID-EXIT-A":
            return _complete_history(oid, sym, "SELL", 87.5)
        raise AssertionError(f"unexpected order {oid}")

    facade.order_history.side_effect = _hist
    facade.get_virtual_contract_note.return_value = [
        _note_row(sym, "BUY", 20.0),
        _note_row(sym, "SELL", 22.0),
    ]
    out = reconcile_closed_trade_from_kite(
        MagicMock(), "TRD-A", facade,
        extra_fills=[{"kite_order_id": "OID-EXIT-A", "leg_order": 1, "quantity": 65}],
    )
    assert out is not None
    assert set(out["kite_order_ids"]) == {"OID-ENTRY-A", "OID-EXIT-A"}
    assert out["gross_pnl"] == -812.5  # BUY (87.5-100)*65
    assert out["total_charges"] == 42.0
    trd.update_pnl.assert_called_once_with("TRD-A", -812.5, 42.0, -854.5)


def test_aborts_when_extra_fill_belongs_to_another_trade(mocker):
    sym = "NIFTY26MAY23000CE"
    rows = [
        _broker_row(oid="OID-ENTRY-A", trade_id="TRD-A", leg=1, op="ENTRY", symbol=sym),
    ]
    legs = [{
        "leg_order": 1, "executed": True, "action": "BUY",
        "fill_price": 100.0, "exit_price": 90.0,
        "lots": 1, "lot_size": 65,
    }]
    other = {
        "OID-EXIT-B": [_broker_row(
            oid="OID-EXIT-B", trade_id="TRD-B", leg=1, op="EXIT", symbol=sym,
        )],
    }
    broker, trd = _patch_repos(
        mocker, has_entry=True, broker_rows=rows, legs=legs, other_by_oid=other,
    )
    facade = MagicMock()
    out = reconcile_closed_trade_from_kite(
        MagicMock(), "TRD-A", facade,
        extra_fills=[{"kite_order_id": "OID-EXIT-B", "leg_order": 1}],
    )
    assert out is None
    facade.get_virtual_contract_note.assert_not_called()
    trd.update_pnl.assert_not_called()


def test_ignores_rollback_and_incomplete_rows(mocker):
    sym = "NIFTY26MAY23000CE"
    rows = [
        _broker_row(oid="OID-ENTRY-A", trade_id="TRD-A", leg=1, op="ENTRY", symbol=sym),
        _broker_row(oid="OID-EXIT-A", trade_id="TRD-A", leg=1, op="EXIT", symbol=sym),
        _broker_row(oid="OID-RB", trade_id="TRD-A", leg=1, op="ROLLBACK", symbol=sym),
        _broker_row(
            oid="OID-PEND", trade_id="TRD-A", leg=1, op="EXIT", symbol=sym,
            status="OPEN",
        ),
    ]
    legs = [{
        "leg_order": 1, "executed": True, "action": "SELL",
        "fill_price": 80.0, "exit_price": 70.0,
        "lots": 1, "lot_size": 65,
    }]
    _patch_repos(mocker, has_entry=True, broker_rows=rows, legs=legs)
    facade = MagicMock()
    facade.order_history.side_effect = lambda oid: _complete_history(
        oid, sym, "SELL" if "EXIT" in oid else "BUY",
        80.0 if "ENTRY" in oid else 70.0,
    )
    facade.get_virtual_contract_note.return_value = [
        _note_row(sym, "BUY", 20.0),
        _note_row(sym, "SELL", 20.0),
    ]
    out = reconcile_closed_trade_from_kite(MagicMock(), "TRD-A", facade)
    assert out is not None
    assert set(out["kite_order_ids"]) == {"OID-ENTRY-A", "OID-EXIT-A"}
    note_ids = [p["order_id"] for p in facade.get_virtual_contract_note.call_args.args[0]]
    assert "OID-RB" not in note_ids
    assert "OID-PEND" not in note_ids


def test_keeps_estimate_when_virtual_note_size_mismatches(mocker):
    sym = "NIFTY26MAY23000CE"
    rows = [
        _broker_row(oid="OID-ENTRY-A", trade_id="TRD-A", leg=1, op="ENTRY", symbol=sym),
        _broker_row(oid="OID-EXIT-A", trade_id="TRD-A", leg=1, op="EXIT", symbol=sym),
    ]
    legs = [{
        "leg_order": 1, "executed": True, "action": "BUY",
        "fill_price": 100.0, "exit_price": 90.0,
        "lots": 1, "lot_size": 65,
    }]
    _, trd = _patch_repos(mocker, has_entry=True, broker_rows=rows, legs=legs)
    facade = MagicMock()
    facade.order_history.side_effect = lambda oid: _complete_history(
        oid, sym, "BUY" if "ENTRY" in oid else "SELL", 100.0,
    )
    facade.get_virtual_contract_note.return_value = [_note_row(sym, "BUY", 20.0)]
    out = reconcile_closed_trade_from_kite(MagicMock(), "TRD-A", facade)
    assert out is None
    trd.update_pnl.assert_not_called()


def test_two_legs_use_their_own_order_ids(mocker):
    ce = "NIFTY26MAY23000CE"
    pe = "NIFTY26MAY23000PE"
    rows = [
        _broker_row(oid="E-CE", trade_id="TRD-A", leg=1, op="ENTRY", symbol=ce),
        _broker_row(oid="X-CE", trade_id="TRD-A", leg=1, op="EXIT", symbol=ce),
        _broker_row(oid="E-PE", trade_id="TRD-A", leg=2, op="ENTRY", symbol=pe),
        _broker_row(oid="X-PE", trade_id="TRD-A", leg=2, op="EXIT", symbol=pe),
    ]
    legs = [
        {
            "leg_order": 1, "executed": True, "action": "BUY",
            "fill_price": 100.0, "exit_price": 90.0,
            "lots": 1, "lots_actual": 1, "lot_size": 65,
            "fill_time": None, "exit_time": None,
        },
        {
            "leg_order": 2, "executed": True, "action": "SELL",
            "fill_price": 80.0, "exit_price": 70.0,
            "lots": 1, "lots_actual": 1, "lot_size": 65,
            "fill_time": None, "exit_time": None,
        },
    ]
    _, trd = _patch_repos(mocker, has_entry=True, broker_rows=rows, legs=legs)
    hist = {
        "E-CE": _complete_history("E-CE", ce, "BUY", 100.0),
        "X-CE": _complete_history("X-CE", ce, "SELL", 90.0),
        "E-PE": _complete_history("E-PE", pe, "SELL", 80.0),
        "X-PE": _complete_history("X-PE", pe, "BUY", 70.0),
    }
    facade = MagicMock()
    facade.order_history.side_effect = lambda oid: hist[oid]
    facade.get_virtual_contract_note.return_value = [
        _note_row(ce, "BUY", 10.0),
        _note_row(ce, "SELL", 10.0),
        _note_row(pe, "SELL", 10.0),
        _note_row(pe, "BUY", 10.0),
    ]
    out = reconcile_closed_trade_from_kite(MagicMock(), "TRD-A", facade)
    assert out is not None
    assert set(out["kite_order_ids"]) == {"E-CE", "X-CE", "E-PE", "X-PE"}
    # BUY CE (90-100)*65 = -650; SELL PE (80-70)*65 = +650
    assert out["gross_pnl"] == 0.0
    assert out["total_charges"] == 40.0
    assert out["net_pnl"] == -40.0
    hist_ids = [c.args[0] for c in facade.order_history.call_args_list]
    assert set(hist_ids) == {"E-CE", "X-CE", "E-PE", "X-PE"}
    note_ids = [p["order_id"] for p in facade.get_virtual_contract_note.call_args.args[0]]
    assert set(note_ids) == {"E-CE", "X-CE", "E-PE", "X-PE"}
    trd.update_pnl.assert_called_once_with("TRD-A", 0.0, 40.0, -40.0)


def test_qty_weighted_average_for_split_entry_fills(mocker):
    sym = "NIFTY26MAY23000CE"
    rows = [
        _broker_row(oid="E1", trade_id="TRD-A", leg=1, op="ENTRY", symbol=sym, qty=30),
        _broker_row(oid="E2", trade_id="TRD-A", leg=1, op="ENTRY", symbol=sym, qty=35),
        _broker_row(oid="X1", trade_id="TRD-A", leg=1, op="EXIT", symbol=sym, qty=65),
    ]
    legs = [{
        "leg_order": 1, "executed": True, "action": "BUY",
        "fill_price": 100.0, "exit_price": 90.0,
        "lots": 1, "lots_actual": 1, "lot_size": 65,
        "fill_time": None, "exit_time": None,
    }]
    _, trd = _patch_repos(mocker, has_entry=True, broker_rows=rows, legs=legs)
    facade = MagicMock()

    def _hist(oid):
        if oid == "E1":
            return _complete_history(oid, sym, "BUY", 100.0, qty=30)
        if oid == "E2":
            return _complete_history(oid, sym, "BUY", 110.0, qty=35)
        if oid == "X1":
            return _complete_history(oid, sym, "SELL", 90.0, qty=65)
        raise AssertionError(oid)

    facade.order_history.side_effect = _hist
    facade.get_virtual_contract_note.return_value = [
        _note_row(sym, "BUY", 20.0),
        _note_row(sym, "BUY", 20.0),
        _note_row(sym, "SELL", 20.0),
    ]
    out = reconcile_closed_trade_from_kite(MagicMock(), "TRD-A", facade)
    assert out is not None
    # (100*30 + 110*35) / 65 = 105.3846…; BUY (90-105.3846)*65 = -1000
    assert out["gross_pnl"] == -1000.0
    trd.update_pnl.assert_called_once_with("TRD-A", -1000.0, 60.0, -1060.0)


def test_aborts_on_tradingsymbol_mismatch(mocker):
    rows = [
        _broker_row(
            oid="OID-ENTRY-A", trade_id="TRD-A", leg=1, op="ENTRY",
            symbol="NIFTY26MAY23000CE",
        ),
        _broker_row(
            oid="OID-EXIT-A", trade_id="TRD-A", leg=1, op="EXIT",
            symbol="NIFTY26MAY23000CE",
        ),
    ]
    legs = [{
        "leg_order": 1, "executed": True, "action": "BUY",
        "fill_price": 100.0, "exit_price": 90.0,
        "lots": 1, "lot_size": 65,
    }]
    _, trd = _patch_repos(mocker, has_entry=True, broker_rows=rows, legs=legs)
    facade = MagicMock()
    facade.order_history.side_effect = lambda oid: _complete_history(
        oid, "BANKNIFTY26MAY52000CE", "BUY" if "ENTRY" in oid else "SELL", 100.0,
    )
    out = reconcile_closed_trade_from_kite(MagicMock(), "TRD-A", facade)
    assert out is None
    facade.get_virtual_contract_note.assert_not_called()
    trd.update_pnl.assert_not_called()


def test_aborts_when_history_order_id_does_not_match_request(mocker):
    sym = "NIFTY26MAY23000CE"
    rows = [
        _broker_row(oid="OID-ENTRY-A", trade_id="TRD-A", leg=1, op="ENTRY", symbol=sym),
        _broker_row(oid="OID-EXIT-A", trade_id="TRD-A", leg=1, op="EXIT", symbol=sym),
    ]
    legs = [{
        "leg_order": 1, "executed": True, "action": "BUY",
        "fill_price": 100.0, "exit_price": 90.0,
        "lots": 1, "lot_size": 65,
    }]
    _, trd = _patch_repos(mocker, has_entry=True, broker_rows=rows, legs=legs)
    facade = MagicMock()
    facade.order_history.return_value = _complete_history(
        "SOME-OTHER-OID", sym, "BUY", 100.0,
    )
    out = reconcile_closed_trade_from_kite(MagicMock(), "TRD-A", facade)
    assert out is None
    facade.get_virtual_contract_note.assert_not_called()
    trd.update_pnl.assert_not_called()


def test_date_scan_skips_paper_and_dedupes(mocker):
    db = MagicMock()
    db.fetch_all.return_value = [
        {"trade_id": "TRD-A"},
        {"trade_id": "TRD-PAPER"},
        {"trade_id": "TRD-A"},
        {"trade_id": "TRD-B"},
    ]
    broker = MagicMock()
    broker.has_entry_kite_fills_for_trade.side_effect = lambda tid: tid != "TRD-PAPER"
    mocker.patch(
        "lifecycle.zerodha_close_pnl.BrokerOrderRepo",
        return_value=broker,
    )
    day = date(2026, 9, 28)
    ids = closed_zerodha_trade_ids_on(db, day)
    assert ids == ["TRD-A", "TRD-B"]
    sql, params = db.fetch_all.call_args[0]
    assert "CAST(t.closed_on AS DATE) = ?" in sql
    assert params == [day]
    broker.has_entry_kite_fills_for_trade.assert_any_call("TRD-PAPER")


def test_date_reconcile_runs_one_trade_id_at_a_time(mocker):
    mocker.patch(
        "lifecycle.zerodha_close_pnl.closed_zerodha_trade_ids_on",
        return_value=["TRD-A", "TRD-B"],
    )
    seen: list[str] = []

    def _rec(_db, tid, _facade, extra_fills=None):
        seen.append(tid)
        return {"trade_id": tid, "kite_order_ids": [f"OID-{tid}"]}

    rec = mocker.patch(
        "lifecycle.zerodha_close_pnl.reconcile_closed_trade_from_kite",
        side_effect=_rec,
    )
    facade = MagicMock()
    out = reconcile_closed_zerodha_trades_on(
        MagicMock(), facade, date(2026, 9, 28),
    )
    assert seen == ["TRD-A", "TRD-B"]
    assert rec.call_count == 2
    assert rec.call_args_list[0].args[1] == "TRD-A"
    assert rec.call_args_list[1].args[1] == "TRD-B"
    assert rec.call_args_list[0].args[2] is facade
    assert [r["trade_id"] for r in out] == ["TRD-A", "TRD-B"]
    assert all(r["applied"] for r in out)
    facade.order_history.assert_not_called()
    facade.get_virtual_contract_note.assert_not_called()


def test_date_reconcile_dry_run_does_not_call_kite(mocker):
    mocker.patch(
        "lifecycle.zerodha_close_pnl.closed_zerodha_trade_ids_on",
        return_value=["TRD-A"],
    )
    rec = mocker.patch("lifecycle.zerodha_close_pnl.reconcile_closed_trade_from_kite")
    facade = MagicMock()
    out = reconcile_closed_zerodha_trades_on(
        MagicMock(), facade, date(2026, 9, 28), dry_run=True,
    )
    assert out == [{"trade_id": "TRD-A", "dry_run": True, "applied": False}]
    rec.assert_not_called()
    facade.order_history.assert_not_called()
    facade.get_virtual_contract_note.assert_not_called()
