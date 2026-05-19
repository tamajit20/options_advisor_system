"""Analyze trade performance — wins/losses, strategy mix, entry conditions, exit reasons."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import date

from database.connection import SQLServerConnection


def fmt(n):
    if n is None:
        return "n/a"
    return f"{float(n):,.0f}"


RS = "Rs."


def main():
    db = SQLServerConnection()

    print("=" * 70)
    print("TRADE PERFORMANCE REVIEW")
    print("=" * 70)

    # 1. Overall trade counts
    counts = db.fetch_all(
        "SELECT status, COUNT(*) AS n, SUM(net_pnl) AS total_pnl "
        "FROM options_trades GROUP BY status ORDER BY status"
    )
    print("\n=== TRADE COUNTS BY STATUS ===")
    for r in counts:
        print(f"  {r['status']:<15} n={r['n']:>4}  total_net_pnl={fmt(r['total_pnl'])}")

    print("\n=== SUGGESTION COUNTS BY STATUS ===")
    sc = db.fetch_all(
        "SELECT status, COUNT(*) AS n FROM options_suggestions GROUP BY status ORDER BY status"
    )
    for r in sc:
        print(f"  {r['status']:<15} n={r['n']}")

    # 2. Closed trades — win/loss breakdown
    closed = db.fetch_all(
        "SELECT t.trade_id, t.suggestion_id, t.trade_name, t.status, t.executed_on, "
        "t.closed_on, t.net_credit_actual, t.actual_max_profit, t.actual_max_loss, "
        "t.gross_pnl, t.net_pnl, t.total_charges, t.spot_at_execution, "
        "s.strategy_type, s.underlying, s.data_source, s.trigger_type, "
        "s.confidence_score, s.conditions_json, s.spot_at_generation, "
        "s.expiry_date, s.dte, s.strategy "
        "FROM options_trades t "
        "LEFT JOIN options_suggestions s ON s.suggestion_id = t.suggestion_id "
        "WHERE t.status IN ('CLOSED', 'EXPIRED') "
        "ORDER BY t.closed_on DESC"
    )

    print(f"\n=== CLOSED TRADES: {len(closed)} ===")
    if not closed:
        print("  No closed trades yet — system is still all open or no trades executed.")
        db.close()
        return

    wins = [t for t in closed if (t["net_pnl"] or 0) > 0]
    losses = [t for t in closed if (t["net_pnl"] or 0) < 0]
    flat = [t for t in closed if (t["net_pnl"] or 0) == 0]

    total_net = sum((t["net_pnl"] or 0) for t in closed)
    total_charges = sum((t["total_charges"] or 0) for t in closed)
    total_gross = sum((t["gross_pnl"] or 0) for t in closed)

    print(f"  Wins:    {len(wins):>4}  net ₹{fmt(sum(t['net_pnl'] for t in wins))}")
    print(f"  Losses:  {len(losses):>4}  net ₹{fmt(sum(t['net_pnl'] for t in losses))}")
    print(f"  Flat:    {len(flat):>4}")
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    print(f"  Win rate: {win_rate:.1f}%")
    print(f"  Gross PnL:   ₹{fmt(total_gross)}")
    print(f"  Charges:     ₹{fmt(total_charges)}")
    print(f"  Net PnL:     ₹{fmt(total_net)}")
    if wins:
        print(f"  Avg win:     ₹{fmt(sum(t['net_pnl'] for t in wins)/len(wins))}")
    if losses:
        print(f"  Avg loss:    ₹{fmt(sum(t['net_pnl'] for t in losses)/len(losses))}")
    if wins and losses:
        rr = abs(sum(t['net_pnl'] for t in wins)/len(wins)) / abs(sum(t['net_pnl'] for t in losses)/len(losses))
        print(f"  Avg R:R:     {rr:.2f}")

    # 3. By strategy
    print("\n=== BY STRATEGY ===")
    by_strat = defaultdict(list)
    for t in closed:
        by_strat[t.get("strategy_type") or "?"].append(t)
    for strat, ts in sorted(by_strat.items(), key=lambda x: -sum(t['net_pnl'] or 0 for t in x[1])):
        wins_s = sum(1 for t in ts if (t["net_pnl"] or 0) > 0)
        net_s = sum((t["net_pnl"] or 0) for t in ts)
        print(f"  {strat:<22} n={len(ts):>3}  wins={wins_s:>3}/{len(ts):<3}  net ₹{fmt(net_s)}")

    # 4. By data_source / trigger_type
    print("\n=== BY DATA SOURCE / TRIGGER ===")
    by_src = defaultdict(list)
    for t in closed:
        key = f"{t.get('data_source') or '?'}/{t.get('trigger_type') or '?'}"
        by_src[key].append(t)
    for key, ts in by_src.items():
        wins_s = sum(1 for t in ts if (t["net_pnl"] or 0) > 0)
        net_s = sum((t["net_pnl"] or 0) for t in ts)
        print(f"  {key:<25} n={len(ts):>3}  wins={wins_s}/{len(ts)}  net ₹{fmt(net_s)}")

    # 5. By underlying
    print("\n=== BY UNDERLYING ===")
    by_und = defaultdict(list)
    for t in closed:
        by_und[t.get("underlying") or "?"].append(t)
    for u, ts in by_und.items():
        wins_s = sum(1 for t in ts if (t["net_pnl"] or 0) > 0)
        net_s = sum((t["net_pnl"] or 0) for t in ts)
        print(f"  {u:<15} n={len(ts):>3}  wins={wins_s}/{len(ts)}  net ₹{fmt(net_s)}")

    # 6. Loss patterns — losses table
    print("\n=== LOSING TRADES (last 15) ===")
    print(f"  {'trade_id':<22} {'underlying':<10} {'strategy':<22} "
          f"{'maxL':>8} {'pnl':>8} {'dte':>4} {'reason'}")
    for t in sorted(losses, key=lambda x: x["net_pnl"] or 0)[:15]:
        reason = ""
        cj = t.get("conditions_json")
        if cj:
            try:
                checks = json.loads(cj) if isinstance(cj, str) else cj
                if isinstance(checks, list):
                    for c in checks:
                        if "trend" in (c.get("label") or "").lower():
                            d = c.get("detail") or ""
                            if "Trend:" in d:
                                t_label = d.split("Trend:", 1)[1].split("(")[0].split("·")[0].strip()
                                reason = f"trend={t_label}"
                            break
            except Exception:
                pass
        print(f"  {t['trade_id']:<22} {t.get('underlying','?'):<10} "
              f"{(t.get('strategy') or t.get('strategy_type','?')):<22} "
              f"{fmt(t['actual_max_loss']):>8} {fmt(t['net_pnl']):>8} "
              f"{(t.get('dte') or 0):>4} {reason}")

    # 7. Spot move vs entry
    print("\n=== SPOT MOVE (entry -> close) FOR LOSING TRADES ===")
    from database.models import SpotEodRepo
    sp = SpotEodRepo(db)
    big_moves = []
    for t in losses[:30]:
        entry_spot = float(t.get("spot_at_execution") or t.get("spot_at_generation") or 0)
        if not entry_spot or not t.get("closed_on"):
            continue
        # latest spot on/before closed_on
        close_row = sp.for_date(t["underlying"], t["closed_on"].date() if hasattr(t["closed_on"], "date") else t["closed_on"])
        if not close_row:
            continue
        close_spot = float(close_row["close_price"])
        move = (close_spot - entry_spot) / entry_spot * 100
        big_moves.append((t["trade_id"], t.get("strategy_type"), t.get("underlying"), entry_spot, close_spot, move, t["net_pnl"]))
    big_moves.sort(key=lambda r: abs(r[5]), reverse=True)
    print(f"  {'trade_id':<22} {'sym':<8} {'strat':<22} {'entry':>9} {'close':>9} {'move%':>7} {'pnl':>8}")
    for tid, strat, sym, e, c, m, pnl in big_moves[:15]:
        print(f"  {tid:<22} {sym:<8} {strat:<22} {e:>9.1f} {c:>9.1f} {m:>+6.2f}% {fmt(pnl):>8}")

    # 8. Confidence vs outcome
    print("\n=== CONFIDENCE BUCKET vs WIN RATE ===")
    buckets = defaultdict(list)
    for t in closed:
        score = t.get("confidence_score")
        if score is None:
            continue
        b = f"{int(score)}"
        buckets[b].append(t)
    for b in sorted(buckets.keys()):
        ts = buckets[b]
        wins_s = sum(1 for t in ts if (t["net_pnl"] or 0) > 0)
        net_s = sum((t["net_pnl"] or 0) for t in ts)
        wr = wins_s / len(ts) * 100 if ts else 0
        print(f"  score={b:<4} n={len(ts):>3}  win_rate={wr:>5.1f}%  net ₹{fmt(net_s)}")

    # 9. Exit decisions from notifications
    print("\n=== EXIT NOTIFICATION TYPES (for closed/expired trades) ===")
    exit_types = Counter()
    for t in closed:
        rows = db.fetch_all(
            "SELECT TOP 5 notif_type FROM options_notifications "
            "WHERE related_trade_id = ? "
            "  AND (notif_type LIKE '%EXIT%' "
            "    OR notif_type IN ('SL_TRIGGER','TARGET_HIT','SL_HIT','TAKE_PROFIT','TIME_DECAY_DONE','PRE_BREACH_WARNING','ADVERSE_MOVE_WARNING')) "
            "ORDER BY created_at DESC",
            [t["trade_id"]],
        )
        for r in rows:
            exit_types[r["notif_type"]] += 1
    for k, n in exit_types.most_common():
        print(f"  {k:<25} {n}")

    db.close()


if __name__ == "__main__":
    main()
