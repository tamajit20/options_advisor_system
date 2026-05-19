"""Deep dive: active trades MTM, VOID reason, indicators on losing trades, strategy mix vs trend."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict

from database.connection import SQLServerConnection


def fmt(n):
    if n is None:
        return "n/a"
    try:
        return f"{float(n):,.0f}"
    except Exception:
        return str(n)


def main():
    db = SQLServerConnection()

    # ---- Active trades + unrealized MTM ----
    print("=" * 80)
    print("ACTIVE TRADES (open positions)")
    print("=" * 80)
    active = db.fetch_all(
        "SELECT t.trade_id, t.suggestion_id, t.executed_on, t.position_type, "
        "t.net_credit_actual, t.actual_max_profit, t.actual_max_loss, "
        "t.actual_stop_loss_level, t.spot_at_execution, t.daily_status, "
        "t.exit_instruction, "
        "s.strategy, s.strategy_type, s.underlying, s.dte, s.expiry_date, "
        "s.data_source, s.trigger_type, s.confidence_score "
        "FROM options_trades t LEFT JOIN options_suggestions s ON s.suggestion_id=t.suggestion_id "
        "WHERE t.status='ACTIVE' ORDER BY t.executed_on DESC"
    )
    for t in active:
        print(
            f"\n {t['trade_id']}  exec={t['executed_on']}  {t.get('underlying')}/{t.get('strategy')}  "
            f"DTE={t.get('dte')}  src={t.get('data_source')}/{t.get('trigger_type')}"
        )
        print(
            f"   net_credit={fmt(t['net_credit_actual'])}  max_profit={fmt(t['actual_max_profit'])}  "
            f"max_loss={fmt(t['actual_max_loss'])}  SL_level={fmt(t['actual_stop_loss_level'])}  "
            f"spot@exec={fmt(t['spot_at_execution'])}"
        )
        print(f"   daily_status={t.get('daily_status')}  exit_instr={t.get('exit_instruction')}")
        # Sum leg unrealized pnl
        legs = db.fetch_all(
            "SELECT leg_order, fill_price, exit_price, leg_pnl, lots_actual FROM options_trade_legs "
            "WHERE trade_id=? ORDER BY leg_order",
            [t["trade_id"]],
        )
        for L in legs:
            print(
                f"     leg#{L['leg_order']}  fill={fmt(L['fill_price'])}  "
                f"exit={fmt(L['exit_price'])}  pnl={fmt(L['leg_pnl'])}  lots={L['lots_actual']}"
            )

    # ---- VOID trades ----
    print("\n" + "=" * 80)
    print("VOID TRADES (not filled / cancelled)")
    print("=" * 80)
    voids = db.fetch_all(
        "SELECT t.trade_id, t.suggestion_id, t.executed_on, t.position_type, "
        "t.exit_instruction, s.strategy, s.underlying, s.data_source, s.trigger_type "
        "FROM options_trades t LEFT JOIN options_suggestions s ON s.suggestion_id=t.suggestion_id "
        "WHERE t.status='VOID' ORDER BY t.executed_on DESC"
    )
    for t in voids:
        print(
            f"  {t['trade_id']}  exec={t['executed_on']}  {t.get('underlying')}/{t.get('strategy')}  "
            f"src={t.get('data_source')}/{t.get('trigger_type')}  reason={t.get('exit_instruction')}"
        )

    # ---- Strategy mix across ALL suggestions (incl. NO_SUGGESTION etc.) ----
    print("\n" + "=" * 80)
    print("SUGGESTION STRATEGY MIX (EXECUTED + IGNORED)")
    print("=" * 80)
    sm = db.fetch_all(
        "SELECT strategy, strategy_type, status, COUNT(*) AS n "
        "FROM options_suggestions WHERE status IN ('EXECUTED','IGNORED','PENDING') "
        "GROUP BY strategy, strategy_type, status ORDER BY strategy, status"
    )
    for r in sm:
        print(f"  {r['strategy_type']:<12} {r['strategy']:<22} {r['status']:<12} n={r['n']}")

    # ---- Closed trade root cause from conditions_json ----
    print("\n" + "=" * 80)
    print("LOSING TRADES — entry signal anatomy")
    print("=" * 80)
    closed = db.fetch_all(
        "SELECT t.trade_id, t.suggestion_id, t.executed_on, t.closed_on, t.net_pnl, "
        "s.strategy, s.underlying, s.dte, s.expiry_date, s.confidence_score, "
        "s.conditions_json, s.data_source, s.trigger_type, s.spot_at_generation, "
        "t.spot_at_execution, t.actual_stop_loss_level "
        "FROM options_trades t LEFT JOIN options_suggestions s ON s.suggestion_id=t.suggestion_id "
        "WHERE t.status IN ('CLOSED','EXPIRED') ORDER BY t.closed_on DESC"
    )
    for t in closed:
        print(
            f"\n-- {t['trade_id']}  {t.get('underlying')}/{t.get('strategy')}  "
            f"DTE@entry={t.get('dte')}  net_pnl={fmt(t['net_pnl'])} --"
        )
        print(
            f"   src={t.get('data_source')}/{t.get('trigger_type')}  "
            f"confidence={t.get('confidence_score')}  "
            f"spot@gen={fmt(t.get('spot_at_generation'))}  spot@exec={fmt(t.get('spot_at_execution'))}  "
            f"SL_level={fmt(t.get('actual_stop_loss_level'))}"
        )
        cj = t.get("conditions_json")
        if cj:
            try:
                checks = json.loads(cj) if isinstance(cj, str) else cj
                if isinstance(checks, list):
                    print("   conditions:")
                    for c in checks:
                        label = c.get("label", "?")
                        det = (c.get("detail") or "").replace("\n", " ")[:120]
                        ok = c.get("ok")
                        print(f"     [{'PASS' if ok else 'WARN'}] {label}: {det}")
            except Exception as e:
                print(f"   (cannot parse conditions_json: {e})")

        # Legs
        legs = db.fetch_all(
            "SELECT tl.leg_order, tl.fill_price, tl.exit_price, tl.leg_pnl, tl.lots_actual, "
            "sl.action, sl.option_type, sl.strike "
            "FROM options_trade_legs tl "
            "LEFT JOIN options_suggestion_legs sl ON sl.id=tl.suggestion_leg_id "
            "WHERE tl.trade_id=? ORDER BY tl.leg_order",
            [t["trade_id"]],
        )
        if legs:
            print("   legs:")
            for L in legs:
                print(
                    f"     #{L['leg_order']} {L.get('action','?'):<4} {L.get('option_type','?')} "
                    f"@{fmt(L.get('strike'))}  fill={fmt(L['fill_price'])}  "
                    f"exit={fmt(L['exit_price'])}  pnl={fmt(L['leg_pnl'])}"
                )

        # Exit notifications
        notifs = db.fetch_all(
            "SELECT TOP 10 notif_type, title, body, severity, created_at FROM options_notifications "
            "WHERE related_trade_id=? ORDER BY created_at DESC",
            [t["trade_id"]],
        )
        if notifs:
            print("   notifications:")
            for n in notifs[:6]:
                msg = (n.get('body') or n.get('title') or '')[:120]
                print(f"     [{n['created_at']}] {n['notif_type']} ({n['severity']}): {msg}")

    db.close()


if __name__ == "__main__":
    main()
