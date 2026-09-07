"""Explicit list of auto-execution actions.

Edit this file when adding a new auto feature. Nothing is auto-discovered.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

from lifecycle.auto_execution.close_on_milestone import CloseOnLossMilestone
from lifecycle.auto_execution.types import AutoExecAction

# Entry, TARGET_HIT / profit booking, and hard SL stay off this list on purpose.
REGISTERED_ACTIONS: Tuple[AutoExecAction, ...] = (
    CloseOnLossMilestone(),
)


def matching_actions(notif_type: str) -> List[AutoExecAction]:
    key = str(notif_type or "").upper()
    return [
        action for action in REGISTERED_ACTIONS
        if key in action.notif_types and action.enabled()
    ]


def registered_notif_types() -> Sequence[str]:
    out: List[str] = []
    for action in REGISTERED_ACTIONS:
        out.extend(sorted(action.notif_types))
    return tuple(dict.fromkeys(out))
