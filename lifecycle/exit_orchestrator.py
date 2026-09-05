"""
lifecycle/exit_orchestrator.py
==============================

Daily exit-decision orchestrator. For each open trade:
    1. Load trade + legs
    2. Get current chain mid prices
    3. Run engine.exit_engine.evaluate_exit
    4. Update trade.daily_status + exit_instruction
    5. On TAKE_PROFIT / SL_HIT / EXPIRE, mark closed (P&L computed at close)
    6. Emit notification on non-HOLD decisions
"""

from __future__ import annotations

import logging
from datetime import date

from contracts import Notification
from config import STRATEGY_CONFIG
from database.connection import SQLServerConnection
from database.models import EventCalendarRepo, FoEodRepo, NotificationRepo, SpotEodRepo, TradeRepo
from database.runtime_flags import FLAG_CIRCUIT_BREAKER_ACTIVE, RuntimeFlagsRepo
from engine.adverse_move_advisor import assess_adverse_move
from engine.circuit_breaker import check_daily_pnl_breach
from engine.exit_engine import evaluate_exit
from engine.exit_pricing import (
    aligned_current_chain,
    expiry_date,
    expiry_iso,
    leg_close_pnl,
    sanitized_close_price as _sanitized_close_price,
    unique_leg_expiries,
)
from utils import days_between, now_ist, today_ist
from lifecycle.eod_session import effective_bhav_end_date

logger = logging.getLogger(__name__)


def _close_trade_with_charges(db: SQLServerConnection, trade_id: str,
                              gross_pnl: float) -> None:
    """Close the trade — for now we use the existing P&L and assume charges
    were captured at suggestion time (re-using suggestion estimate)."""
    trd = TradeRepo(db)
    trade = trd.get(trade_id)
    if trade is None:
        return
    charges = float(trade.get("total_charges") or 0.0)
    net = gross_pnl - charges
    trd.close_trade(trade_id, gross=gross_pnl, charges=charges, net=net)


def run_exit_engine(db: SQLServerConnection, trade_date: date | None = None) -> int:
    trade_date = trade_date or effective_bhav_end_date()
    trd = TradeRepo(db)
    fo = FoEodRepo(db)
    notif = NotificationRepo(db)
    spot_repo = SpotEodRepo(db)
    event_repo = EventCalendarRepo(db)

    # Pre-event check (S1): if tomorrow is a high-impact event day, trades that are
    # HOLD today should be downgraded to EXIT_TOMORROW so the user closes before
    # the overnight gap risk materialises.
    from lifecycle.suggestion_engine import _next_trading_day
    next_session = _next_trading_day(trade_date)
    _has_event_tomorrow = event_repo.has_high_impact(next_session, next_session)
    _credit_strategies = frozenset({
        "IRON_CONDOR", "IRON_BUTTERFLY", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD",
        "JADE_LIZARD",
    })

    open_trades = trd.open_trades()
    decisions_made = 0
    aggregate_mtm = 0.0  # sum of current_pnl across all open trades for circuit breaker
    auto_settled = 0     # count of trades auto-closed at DTE=0 (fix B)

    for trade in open_trades:
        trade_id = trade["trade_id"]
        legs = trd.legs(trade_id)
        if not legs:
            continue
        # Look up suggestion legs for strike / option_type / per-leg expiry.
        sug_legs = db.fetch_all(
            "SELECT * FROM options_suggestion_legs WHERE suggestion_id = ? ORDER BY leg_order",
            [trade["suggestion_id"]],
        )
        if not sug_legs:
            continue

        # Phase 2: strategy drives strategy-aware TP and time-decay exit
        sug_row = db.fetch_one(
            "SELECT strategy FROM options_suggestions WHERE suggestion_id = ?",
            [trade["suggestion_id"]],
        )
        strategy = (sug_row or {}).get("strategy", "") or ""

        underlying = sug_legs[0]["symbol"]
        unique_expiries = unique_leg_expiries(sug_legs)
        # Near-expiry DTE (calendars: min of near/far). Fallback to first leg.
        near_expiry = min(unique_expiries) if unique_expiries else expiry_date(
            sug_legs[0]["expiry_date"]
        )
        expiry = near_expiry or sug_legs[0]["expiry_date"]
        dte = days_between(trade_date, expiry)

        # Current chain — skip this trade if no EOD data for today (holiday/weekend).
        # Without chain data, all mid_prices would be 0 which causes evaluate_exit
        # to see full profit on every credit leg and fire spurious TAKE_PROFIT signals.
        # One chain per distinct expiry: calendars must not reuse the near chain
        # for the far leg.
        chains_by_expiry = {}
        missing_expiry = False
        for exp in unique_expiries or [expiry]:
            rows = fo.get_chain(underlying, trade_date, exp)
            if not rows:
                missing_expiry = True
                logger.info(
                    "Exit engine: no chain data for %s/%s on %s — skipping (holiday/weekend)",
                    underlying, exp, trade_date,
                )
                break
            chains_by_expiry[exp] = rows
        if missing_expiry:
            continue

        legs_for_engine = []
        by_order = {l["leg_order"]: l for l in legs}
        for sl in sug_legs:
            tl = by_order.get(sl["leg_order"])
            if not tl or not tl.get("executed"):
                continue
            legs_for_engine.append({
                "leg_order":   sl["leg_order"],
                "action":      sl["action"],
                "strike":      float(sl["strike"]),
                "option_type": sl["option_type"],
                "lots":        sl["lots"],
                "lot_size":    sl["lot_size"],
                "fill_price":  tl.get("fill_price"),
                "expiry_date": sl["expiry_date"],
            })

        if not legs_for_engine:
            continue

        current_chain = aligned_current_chain(legs_for_engine, chains_by_expiry)

        decision = evaluate_exit(
            trade_id=trade_id,
            legs=legs_for_engine,
            current_chain=current_chain,
            entry_net_credit=float(trade.get("net_credit_actual") or 0.0),
            max_profit_rs=float(trade.get("actual_max_profit") or 0.0),
            max_loss_rs=float(trade.get("actual_max_loss") or 0.0),
            sl_level_per_share=trade.get("actual_stop_loss_level"),
            days_to_expiry=dte,
            strategy=strategy,
            as_of=now_ist(),
        )
        decisions_made += 1

        # Update trade — never auto-close. Always wait for the user to record
        # actual broker exit fills via the Close Trade UI. We surface a clear
        # daily_status, an exit instruction containing suggested per-leg
        # closing prices, and notify so the user can act.

        # S1: Pre-event advisory — when a HIGH-impact event is tomorrow, change
        # HOLD → EXIT_TOMORROW so the dashboard shows a prominent warning and a
        # WARNING-severity notification is fired. The trade is NOT automatically
        # closed; the user must act via the Close Trade button as normal.
        if (
            decision.decision == "HOLD"
            and _has_event_tomorrow
            and strategy in _credit_strategies
        ):
            from contracts import ExitDecision
            from utils import now_ist as _now_ist_fn
            decision = ExitDecision(
                trade_id=trade_id,
                decision="EXIT_TOMORROW",
                reason=(
                    f"HIGH-impact event scheduled for {next_session.isoformat()} "
                    "(next session) — consider exiting today to avoid overnight "
                    "gap risk on this short-premium position"
                ),
                as_of=_now_ist_fn(),
            )

        if decision.decision == "HOLD":
            trd.update_status(trade_id, "ACTIVE", "OPEN", None)
            # Adverse-move early warning. Computes the same MTM that
            # evaluate_exit just used (entry_net_credit + current_value)
            # and fires a notification when we cross the warning band.
            entry_credit = float(trade.get("net_credit_actual") or 0.0)
            max_loss_rs  = float(trade.get("actual_max_loss") or 0.0)
            current_value = 0.0
            for leg, crow in zip(legs_for_engine, current_chain):
                mid = float(crow.get("mid_price") or 0.0)
                qty = int(leg["lots"]) * int(leg["lot_size"])
                sign = -1.0 if leg["action"] == "SELL" else 1.0
                current_value += sign * mid * qty
            current_pnl = entry_credit + current_value
            aggregate_mtm += current_pnl
            advice = assess_adverse_move(
                current_pnl=current_pnl,
                max_loss_rs=max_loss_rs,
                strategy=strategy,
            )
            _long_vol = frozenset(
                (STRATEGY_CONFIG.get("long_premium_thesis_exit") or {}).get("strategies")
                or ["LONG_STRADDLE", "LONG_STRANGLE"]
            )
            if advice is not None and strategy not in _long_vol:
                notif.insert(Notification(
                    created_at=now_ist(),
                    notif_type="ADVERSE_MOVE_WARNING",
                    severity="INFO",
                    title=(
                        f"{trade.get('trade_name') or trade_id}: "
                        f"{advice.headline}"
                    ),
                    body=advice.recovery_hint
                           + "\n[INFO ONLY — no mandatory exit; act only on loss limit or sell signal.]",
                    related_trade_id=trade_id,
                ))
        else:
            # Look up underlying spot for this trade_date so we can
            #   (a) sanity-check option premiums (fix E)
            #   (b) cash-settle at intrinsic if DTE=0 (fix B)
            spot_row = spot_repo.for_date(underlying, trade_date)
            spot_close = float(spot_row["close_price"]) if spot_row else None

            # Build per-leg suggested closing prices (mid of latest chain,
            # falling back to intrinsic if the chain row is clearly bogus).
            suggested_lines = []
            est_gross = 0.0
            for leg, crow in zip(legs_for_engine, current_chain):
                raw_mid = float(crow.get("mid_price") or 0.0)
                close_px, src = _sanitized_close_price(
                    option_type=leg["option_type"], strike=float(leg["strike"]),
                    raw_mid=raw_mid, spot=spot_close,
                )
                if src == "intrinsic_fallback":
                    logger.warning(
                        "exit_engine: bogus mid for %s %s %s%s (raw=%.2f) — using intrinsic %.2f",
                        trade_id, expiry_iso(leg.get("expiry_date")),
                        leg["strike"], leg["option_type"], raw_mid, close_px,
                    )
                fill = float(leg.get("fill_price") or 0.0)
                close_action = "Buy back" if leg["action"] == "SELL" else "Sell back"
                exp_label = expiry_iso(leg.get("expiry_date"))
                suggested_lines.append(
                    f"{close_action} {leg['strike']:g} {leg['option_type']}"
                    f"{(' ' + exp_label) if exp_label else ''} @ ~₹{close_px:.2f}"
                )
                est_gross += leg_close_pnl(
                    action=leg["action"],
                    fill_price=fill,
                    close_price=close_px,
                    lots=int(leg["lots"]),
                    lot_size=int(leg["lot_size"]),
                )
            instruction = (
                f"{decision.reason} | Suggested close: "
                + "; ".join(suggested_lines)
                + f" | Est. P&L ₹{est_gross:.0f}"
                + " | Record actual fills via 'Close Trade'."
            )

            # ── Fix B: auto-settle at DTE=0 ────────────────────────────────
            # When the exit engine says EXPIRE, the option contracts will be
            # cash-settled by the exchange regardless of any user action.
            # Leaving the trade row in ACTIVE indefinitely (as happened with
            # TRD-20260506-002 and TRD-20260507-001 in prod) corrupts the
            # P&L dashboard and the circuit-breaker MTM aggregate. We record
            # the intrinsic-value settlement immediately and mark the trade
            # CLOSED with a clear daily_status of AUTO_SETTLED.
            if decision.decision == "EXPIRE":
                try:
                    by_order_lookup = {l["leg_order"]: l for l in legs}
                    leg_results: list[tuple[int, float, float]] = []
                    settle_gross = 0.0
                    for leg, crow in zip(legs_for_engine, current_chain):
                        order = leg["leg_order"]
                        tl = by_order_lookup.get(order)
                        if not tl or not tl.get("executed"):
                            continue
                        close_px, _ = _sanitized_close_price(
                            option_type=leg["option_type"],
                            strike=float(leg["strike"]),
                            raw_mid=float(crow.get("mid_price") or 0.0),
                            spot=spot_close,
                        )
                        lots = int(tl.get("lots_actual") or leg["lots"])
                        fill = float(tl.get("fill_price") or 0.0)
                        action = leg["action"]
                        leg_pnl = leg_close_pnl(
                            action=action,
                            fill_price=fill,
                            close_price=close_px,
                            lots=lots,
                            lot_size=int(leg["lot_size"]),
                        )
                        settle_gross += leg_pnl
                        leg_results.append((order, close_px, leg_pnl))
                    if leg_results:
                        settle_time = now_ist()
                        for order, close_px, leg_pnl in leg_results:
                            trd.update_leg_exit(
                                trade_id=trade_id, leg_order=order,
                                exit_price=close_px, exit_time=settle_time,
                                leg_pnl=leg_pnl,
                            )
                        charges = float(trade.get("total_charges") or 0.0)
                        net_pnl = settle_gross - charges
                        trd.close_trade(
                            trade_id=trade_id, gross=settle_gross,
                            charges=charges, net=net_pnl,
                        )
                        auto_settled += 1
                        notif.insert(Notification(
                            created_at=settle_time,
                            notif_type="AUTO_SETTLED",
                            severity="CRITICAL",
                            title=(
                                f"{trade.get('trade_name') or trade_id}: "
                                f"auto-settled at expiry "
                                f"(net P&L ₹{net_pnl:+,.0f})"
                            ),
                            body=(
                                f"DTE=0 — cash settled at intrinsic value. "
                                f"Gross ₹{settle_gross:+,.0f}, charges ₹{charges:,.0f}, "
                                f"net ₹{net_pnl:+,.0f}. Review and edit fills via "
                                f"'Close Trade' if your actual broker settlement differs."
                            ),
                            related_trade_id=trade_id,
                        ))
                        logger.info(
                            "exit_engine: auto-settled %s at expiry net=%.0f",
                            trade_id, net_pnl,
                        )
                        # Skip the rest of this trade's loop iteration — we
                        # already wrote a status + notification.
                        continue
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "exit_engine: auto-settle failed for %s; falling back to ACTIVE",
                        trade_id,
                    )

            daily = "EXIT_AT_OPEN" if decision.decision == "EXIT_TOMORROW" else decision.decision
            trd.update_status(trade_id, "ACTIVE", daily, instruction)

            # Notification.
            # Severity policy (fix A — was silently INFO for everything except SL_HIT):
            #   CRITICAL — money on the line right now and ignoring is harmful:
            #              EXPIRE (DTE=0 — must settle), SL_HIT, TAKE_PROFIT (booking
            #              gains is the whole point of the trade).
            #   WARNING  — needs attention within a day:
            #              EXIT_TOMORROW, TIME_DECAY_DONE.
            #   INFO     — informational HOLD outcomes never reach this branch.
            crit_kinds = {"EXPIRE", "SL_HIT", "TAKE_PROFIT", "THESIS_FAIL"}
            warn_kinds = {"EXIT_TOMORROW", "TIME_DECAY_DONE"}
            if decision.decision in crit_kinds:
                sev = "CRITICAL"
            elif decision.decision in warn_kinds:
                sev = "WARNING"
            else:
                sev = "INFO"
            notif.insert(Notification(
                created_at=now_ist(),
                notif_type=f"EXIT_{decision.decision}",
                severity=sev,
                title=f"{trade.get('trade_name') or trade_id}: {decision.decision} — close pending",
                body=instruction,
                related_trade_id=trade_id,
            ))

    db.commit()
    logger.info(
        "Exit engine: %d open trades evaluated, %d auto-settled at expiry",
        decisions_made, auto_settled,
    )

    # Daily P&L circuit breaker. Aggregate MTM is summed only for HOLD
    # trades — anything triggering an exit decision will be closed soon
    # and would only confuse the budget once the user records fills.
    breach = check_daily_pnl_breach(total_pnl_rs=aggregate_mtm)
    if breach is not None:
        logger.warning(
            "circuit_breaker: daily P&L breach ₹%.0f (%.2f%% of capital)",
            breach.total_pnl_rs, breach.pct_of_capital,
        )
        try:
            flags = RuntimeFlagsRepo(db)
            flags.set(FLAG_CIRCUIT_BREAKER_ACTIVE, True, modified_by="exit_engine")
        except Exception:
            logger.exception("circuit_breaker: failed to set runtime flag")
        try:
            notif.insert(Notification(
                created_at=now_ist(),
                notif_type="DAILY_PNL_BREACH",
                severity="CRITICAL",
                title=breach.headline,
                body=(
                    f"Aggregate open-trade MTM ₹{breach.total_pnl_rs:+,.0f} "
                    f"breached the daily limit of –₹{breach.limit_rs:,.0f} "
                    f"({breach.limit_pct:.1f}% of ₹{breach.capital_rs:,.0f}). "
                    "New executions are now blocked. Review open positions "
                    "and clear the `circuit_breaker_active` runtime flag "
                    "manually once you've decided next steps."
                ),
            ))
            db.commit()
        except Exception:
            logger.exception("circuit_breaker: failed to insert notification")

    return decisions_made
