"""Run matching auto-exec actions off the tick thread."""
from __future__ import annotations

import logging
import threading

from contracts import Notification
from database.models import NotificationRepo
from database.thread_db import dedicated_connection
from lifecycle.auto_execution.registry import matching_actions
from lifecycle.auto_execution.types import AutoExecAction, AutoExecContext
from utils import now_ist

logger = logging.getLogger(__name__)


def dispatch_auto_execution(ctx: AutoExecContext) -> None:
    """No-op when no enabled action matches. Otherwise start a worker thread."""
    actions = matching_actions(ctx.notif_type)
    if not actions:
        return
    snapshot = AutoExecContext(
        notif_type=ctx.notif_type,
        trade_id=ctx.trade_id,
        trade_name=ctx.trade_name,
        exits=[dict(e) for e in (ctx.exits or [])],
        as_of=ctx.as_of,
    )
    threading.Thread(
        target=_run_actions,
        args=(snapshot, tuple(actions)),
        name=f"auto-exec-{snapshot.notif_type}-{snapshot.trade_id}",
        daemon=True,
    ).start()


def _run_actions(ctx: AutoExecContext, actions: tuple[AutoExecAction, ...]) -> None:
    for action in actions:
        try:
            with dedicated_connection() as db:
                result = action.run(db, ctx)
                logger.info(
                    "auto-exec %s for %s → %s",
                    action.name, ctx.trade_id, result,
                )
        except Exception as exc:
            logger.exception(
                "auto-exec %s failed for %s", action.name, ctx.trade_id,
            )
            try:
                with dedicated_connection() as db:
                    _notify_failed(db, action, ctx, exc)
            except Exception:
                logger.exception(
                    "auto-exec failure alert failed for %s/%s",
                    action.name, ctx.trade_id,
                )


def _notify_failed(
    db,
    action: AutoExecAction,
    ctx: AutoExecContext,
    exc: BaseException,
) -> None:
    NotificationRepo(db).insert(Notification(
        created_at=now_ist(),
        notif_type=action.failure_notif_type,
        severity="CRITICAL",
        title=f"Auto-execution failed ({action.name}) on {ctx.trade_id}",
        body=(
            f"{exc}. If a broker position is still open, flatten on Kite "
            "and record the close on the dashboard."
        ),
        related_trade_id=ctx.trade_id,
    ))
    db.commit()
