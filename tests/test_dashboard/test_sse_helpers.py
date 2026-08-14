"""Tests for dashboard.sse_helpers."""

from __future__ import annotations

from dashboard.sse_helpers import iter_sse_on_change, json_signature


def test_json_signature_stable():
    a = {"x": 1, "y": [2, 3]}
    b = {"y": [2, 3], "x": 1}
    assert json_signature(a) == json_signature(b)


def test_iter_sse_on_change_emits_on_first_poll(mocker):
    mocker.patch("dashboard.sse_helpers.time.sleep", return_value=None)
    calls = {"n": 0}

    def poll():
        calls["n"] += 1
        return {"n": calls["n"]}

    gen = iter_sse_on_change(poll, 0.5)
    assert ": connected" in next(gen)
    msg = next(gen)
    assert msg.startswith("data: ")
    assert '"n": 1' in msg
