"""Tests for providers/zerodha/order_updates.py"""

from providers.zerodha.order_updates import parse_kite_order_row, is_terminal_status


def test_parse_kite_order_row():
    row = parse_kite_order_row({
        "order_id": "1",
        "status": "COMPLETE",
        "filled_quantity": 50,
        "quantity": 50,
        "average_price": 12.5,
    })
    assert row["status"] == "COMPLETE"
    assert row["filled_quantity"] == 50
    assert row["pending_quantity"] == 0


def test_is_terminal():
    assert is_terminal_status("COMPLETE")
    assert not is_terminal_status("OPEN")


def test_wait_keeps_rest_polling_when_cache_is_open(mocker):
    from providers.zerodha.order_updates import wait_for_order_terminal

    facade = mocker.MagicMock()
    facade.order_history.side_effect = [
        [{"order_id": "1", "status": "OPEN", "filled_quantity": 0, "quantity": 50}],
        [{"order_id": "1", "status": "COMPLETE", "filled_quantity": 50,
          "quantity": 50, "average_price": 10.0}],
    ]
    mocker.patch(
        "providers.zerodha.order_updates.get_order_snapshot",
        return_value={"order_id": "1", "status": "OPEN", "filled_quantity": 0},
    )
    out = wait_for_order_terminal(
        "1", facade=facade, max_wait=2.0, poll_interval=0.01, use_ws_cache=True,
    )
    assert out["status"] == "COMPLETE"
    assert facade.order_history.call_count >= 2
