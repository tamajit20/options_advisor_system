"""Shared shapes for auto-execution. No DB or broker imports at runtime."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, FrozenSet, List

if TYPE_CHECKING:
    from database.connection import SQLServerConnection


@dataclass(frozen=True)
class AutoExecContext:
    """One alert the auto-execution layer may act on."""

    notif_type: str
    trade_id: str
    trade_name: str = ""
    exits: List[dict] = field(default_factory=list)
    as_of: datetime | None = None


class AutoExecAction(ABC):
    """One auto behaviour. Gate with ``enabled()``; keep ``run()`` side-effecting."""

    name: str = ""
    notif_types: FrozenSet[str] = frozenset()
    failure_notif_type: str = "AUTO_EXEC_FAILED"

    def enabled(self) -> bool:
        return False

    @abstractmethod
    def run(self, db: SQLServerConnection, ctx: AutoExecContext) -> str:
        """Return a short channel/status label for logs."""
