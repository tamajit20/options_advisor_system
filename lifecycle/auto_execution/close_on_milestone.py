"""LOSS_MILESTONE_HIT → flatten/book the open trade."""
from __future__ import annotations

from typing import FrozenSet

from database.connection import SQLServerConnection
from database.models import TradeRepo
from engine.sl_threshold import loss_milestone_config
from lifecycle.auto_execution.types import AutoExecAction, AutoExecContext
from lifecycle.trade_executor import close_trade_with_fills
from lifecycle.zerodha_executor import (
    EXECUTION_CHANNEL_ZERODHA,
    ZerodhaExecutionError,
    close_trade_in_zerodha_async,
    trade_execution_channel,
    zerodha_execution_ready,
)


class CloseOnLossMilestone(AutoExecAction):
    name = "close_on_loss_milestone"
    notif_types: FrozenSet[str] = frozenset({"LOSS_MILESTONE_HIT"})
    failure_notif_type = "LOSS_MILESTONE_CLOSE_FAILED"

    def enabled(self) -> bool:
        cfg = loss_milestone_config()
        return bool(cfg.get("enabled") and cfg.get("auto_close", True))

    def run(self, db: SQLServerConnection, ctx: AutoExecContext) -> str:
        trd = TradeRepo(db)
        trade = trd.get(ctx.trade_id)
        if trade is None:
            raise ValueError(f"Unknown trade: {ctx.trade_id}")
        status = str(trade.get("status") or "").upper()
        if status in ("CLOSED", "VOID", "EXPIRED"):
            return "already_closed"

        channel = trade_execution_channel(db, trade)
        if channel == EXECUTION_CHANNEL_ZERODHA:
            if not zerodha_execution_ready(db):
                raise ZerodhaExecutionError(
                    "Zerodha execution is not ready — cannot auto-flatten. "
                    "Enable trade execution, log in, then flatten on Kite."
                )
            close_trade_in_zerodha_async(db, ctx.trade_id)
            return "zerodha"

        if not ctx.exits:
            raise ValueError(f"Manual close of {ctx.trade_id} needs live exit prices")
        close_trade_with_fills(db, ctx.trade_id, list(ctx.exits))
        return "manual"
