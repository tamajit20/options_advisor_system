"""
lifecycle/auto_execution
=======================

The only place that may place or book trades without a dashboard button click.

LiveRiskMonitor (and later other jobs) emit an ``AutoExecContext`` after an
alert. This package looks up registered actions for that ``notif_type`` and
runs them on a background thread with a dedicated DB connection.

Manual-only until explicitly registered here
--------------------------------------------
* Place orders / entry — dashboard **Place orders**
* TARGET_HIT / profit booking — dashboard **Close Trade**
* LOSS_LIMIT_HIT, SL_TRIGGER — alerts only (loss-side SL)

Registered now
--------------
* LOSS_MILESTONE_HIT → close the open trade (Kite flatten or manual LTP book)
* PROFIT_MILESTONE_HIT → close to protect peak profit (same flatten path)

To add a later auto feature: implement ``AutoExecAction`` in this package and
append it to ``REGISTERED_ACTIONS`` in ``registry.py``. Do not put order
logic back into ``live_risk_monitor``.
"""

from lifecycle.auto_execution.registry import REGISTERED_ACTIONS, matching_actions
from lifecycle.auto_execution.runner import dispatch_auto_execution
from lifecycle.auto_execution.types import AutoExecAction, AutoExecContext

__all__ = [
    "AutoExecAction",
    "AutoExecContext",
    "REGISTERED_ACTIONS",
    "dispatch_auto_execution",
    "matching_actions",
]
