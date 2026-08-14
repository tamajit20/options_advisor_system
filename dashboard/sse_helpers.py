"""Shared SSE generators — file-watch and poll-on-change patterns."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterator, Optional


def json_signature(payload: Any) -> str:
    """Stable string for change detection."""
    return json.dumps(payload, sort_keys=True, default=str)


def iter_sse_heartbeat(*, heartbeat_sec: float = 15.0) -> Iterator[str]:
    """Yield SSE heartbeat comments every *heartbeat_sec*."""
    heartbeat_at = time.monotonic()
    while True:
        time.sleep(min(heartbeat_sec, 1.0))
        if time.monotonic() - heartbeat_at >= heartbeat_sec:
            heartbeat_at = time.monotonic()
            yield ": ping\n\n"


def iter_sse_on_change(
    poll_fn: Callable[[], Any],
    poll_sec: float,
    *,
    heartbeat_sec: float = 15.0,
    initial: bool = True,
) -> Iterator[str]:
    """Poll *poll_fn*; emit ``data: …`` only when the JSON signature changes."""
    last_sig: Optional[str] = None
    heartbeat_at = time.monotonic()
    yield ": connected\n\n"
    while True:
        time.sleep(poll_sec)
        try:
            payload = poll_fn()
            sig = json_signature(payload)
            if initial or sig != last_sig:
                last_sig = sig
                initial = False
                yield f"data: {json.dumps(payload, default=str)}\n\n"
        except Exception:
            pass
        if time.monotonic() - heartbeat_at >= heartbeat_sec:
            heartbeat_at = time.monotonic()
            yield ": ping\n\n"
