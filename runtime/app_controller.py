"""Start/stop ws_runner app components from runtime flags (no container restart)."""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional, Protocol

from database.runtime_flags import (
    FLAG_ARB_APP_ENABLED,
    FLAG_BASIS_APP_ENABLED,
    FLAG_OPTIONS_ADVISOR_ENABLED,
    FLAG_SCOUT_APP_ENABLED,
    RuntimeFlagsRepo,
)

logger = logging.getLogger(__name__)


class _Stoppable(Protocol):
    def start(self) -> None: ...
    def stop(self, timeout: float = ...) -> None: ...


class AppRuntimeController:
    """Polls DB flags and starts/stops grouped ws_runner services."""

    def __init__(
        self,
        flags_repo: RuntimeFlagsRepo,
        *,
        poll_interval_sec: float = 30.0,
    ) -> None:
        self._flags = flags_repo
        self._poll_interval = max(5.0, float(poll_interval_sec))
        self._options: List[_Stoppable] = []
        self._scout: List[_Stoppable] = []
        self._arb: List[_Stoppable] = []
        self._basis: List[_Stoppable] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_options: Optional[bool] = None
        self._last_scout: Optional[bool] = None
        self._last_arb: Optional[bool] = None
        self._last_basis: Optional[bool] = None

    def register_options(self, *components: _Stoppable) -> None:
        self._options.extend(components)

    def register_scout(self, *components: _Stoppable) -> None:
        self._scout.extend(components)

    def register_arb(self, *components: _Stoppable) -> None:
        self._arb.extend(components)

    def register_basis(self, *components: _Stoppable) -> None:
        self._basis.extend(components)

    def options_enabled(self) -> bool:
        return self._flags.get_bool(FLAG_OPTIONS_ADVISOR_ENABLED, default=True)

    def scout_enabled(self) -> bool:
        return self._flags.get_bool(FLAG_SCOUT_APP_ENABLED, default=True)

    def arb_enabled(self) -> bool:
        return self._flags.get_bool(FLAG_ARB_APP_ENABLED, default=True)

    def basis_enabled(self) -> bool:
        return self._flags.get_bool(FLAG_BASIS_APP_ENABLED, default=True)

    def apply(self) -> None:
        """Sync component lifecycle to current flags."""
        self._sync_group("options_advisor", self.options_enabled(), self._options, "_last_options")
        self._sync_group("scout", self.scout_enabled(), self._scout, "_last_scout")
        self._sync_group("arb", self.arb_enabled(), self._arb, "_last_arb")
        self._sync_group("basis", self.basis_enabled(), self._basis, "_last_basis")

    def start_polling(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="app-runtime", daemon=True)
        self._thread.start()

    def stop_polling(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            try:
                self.apply()
            except Exception:
                logger.exception("AppRuntimeController: apply failed")

    def _sync_group(self, name: str, enabled: bool, components: List[_Stoppable], last_attr: str) -> None:
        prev = getattr(self, last_attr)
        if prev is not None and prev == enabled:
            return
        setattr(self, last_attr, enabled)
        action = "start" if enabled else "stop"
        for comp in components:
            try:
                if enabled:
                    comp.start()
                else:
                    comp.stop()
            except Exception:
                logger.exception("AppRuntimeController: failed to %s %s", action, type(comp).__name__)
        logger.info("AppRuntimeController: %s %s", name, "enabled" if enabled else "disabled")


def make_app_flag_checkers(flags_repo: RuntimeFlagsRepo) -> dict[str, Callable[[], bool]]:
    """Callables for SubscriptionManager — same flags, shared cache TTL."""
    return {
        "options": lambda: flags_repo.get_bool(FLAG_OPTIONS_ADVISOR_ENABLED, default=True),
        "scout": lambda: flags_repo.get_bool(FLAG_SCOUT_APP_ENABLED, default=True),
        "arb": lambda: flags_repo.get_bool(FLAG_ARB_APP_ENABLED, default=True),
        "basis": lambda: flags_repo.get_bool(FLAG_BASIS_APP_ENABLED, default=True),
    }
