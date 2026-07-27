"""
providers/zerodha/ws_runner_control.py
======================================

Best-effort control of the ``stock_ws_runner`` Docker container from the
dashboard container after a Zerodha login.

When the ws_runner process has crashed or hit Docker's restart limit, saving
a new session file alone is not enough — something must start the container
again. This module uses the Docker Engine HTTP API over the Unix socket
(``/var/run/docker.sock``), which is mounted read-only into
``options_advisor`` on production deployments.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER = "stock_ws_runner"
DEFAULT_SOCKET = "/var/run/docker.sock"


def _docker_api(method: str, path: str) -> tuple[bool, str]:
    """Call the Docker Engine API via curl + Unix socket."""
    socket = os.environ.get("DOCKER_SOCKET", DEFAULT_SOCKET)
    if not os.path.exists(socket):
        return False, f"docker socket not found at {socket}"
    cmd = [
        "curl",
        "-sS",
        "--unix-socket",
        socket,
        "-X",
        method,
        f"http://localhost{path}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"curl exit {proc.returncode}").strip()
        return False, err
    return True, (proc.stdout or "").strip()


def _container_state(name: str) -> Optional[str]:
    ok, body = _docker_api("GET", f"/containers/{name}/json")
    if not ok:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    return str((data.get("State") or {}).get("Status") or "")


def ensure_ws_runner_running(container: str = DEFAULT_CONTAINER) -> Dict[str, Any]:
    """Start ws_runner if it is not already running."""
    state = _container_state(container)
    if state is None:
        return {
            "ok": False,
            "action": "none",
            "reason": "container not found or docker unavailable",
        }
    if state == "running":
        return {"ok": True, "action": "already_running", "state": state}

    ok, msg = _docker_api("POST", f"/containers/{container}/start")
    if ok:
        return {"ok": True, "action": "started", "previous_state": state}
    return {
        "ok": False,
        "action": "start_failed",
        "previous_state": state,
        "error": msg,
    }


def notify_session_updated() -> Dict[str, Any]:
    """Wake the ws_runner container after a Zerodha session is saved."""
    result = ensure_ws_runner_running()
    if result.get("ok"):
        logger.info("ws_runner wake: %s", result.get("action"))
    else:
        logger.warning("ws_runner wake failed: %s", result)
    return result
