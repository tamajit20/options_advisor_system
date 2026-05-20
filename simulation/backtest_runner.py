"""
simulation/backtest_runner.py
=============================

Phase 4: Empirical validation harness for the suggestion + exit pipeline.

Walks through every historical suggestion in the DB, day-by-day, and applies
the LIVE exit rules (engine.exit_engine.evaluate_exit) against each day's
settle prices. Records the simulated exit outcome and aggregates per-strategy
statistics to a CSV.

Why this is useful:
    - Validates that strategy-specific TP fractions (Phase 2) actually improve P&L
    - Validates that TIME_DECAY_DONE exits avoid the worst gamma blowups
    - Quantifies win rate / average P&L / drawdown per strategy
    - Surfaces edge cases (e.g. weekend gaps, missing chain data)

What this does NOT do:
    - Re-evaluate confidence gate (we use suggestions as recorded)
    - Re-run strategy selection with new trend logic (Phase 1 changes don't
      retroactively affect existing suggestions; future runs will reflect them)
    - Account for slippage between suggested and actual fill prices
    - Model real-world manual exit lag (assumes instant exit at settle)

CLI:
    python -m simulation.backtest_runner --start 2025-01-01 --end 2026-04-30

Output:
    backtest_results_<timestamp>.csv  — one row per simulated trade
    Console summary  — per-strategy aggregates
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from config import STRATEGY_CONFIG
from database.connection import SQLServerConnection
from database.models import FoEodRepo, SuggestionRepo
from engine.exit_engine import evaluate_exit
from utils import days_between, now_ist

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-day MTM helper (mirrors live exit_orchestrator computation)
# ---------------------------------------------------------------------------

def _build_chain_lookup(chain_rows: Sequence[dict]) -> Dict[tuple, float]:
    """Map (strike, option_type) -> mid price (settle preferred, close fallback)."""
    return {
        (float(c["strike"]), c["option_type"]):
            float(c.get("settle_price") or c.get("close_price") or 0.0)
        for c in chain_rows
    }


def _legs_for_engine(sug_legs: Sequence[dict]) -> List[dict]:
    """Convert suggestion legs to the dict shape evaluate_exit expects.

    Uses suggested_price as the assumed fill (we have no actual fill in
    pure-suggestion records that were never executed).
    """
    out: List[dict] = []
    for leg in sug_legs:
        out.append({
            "action":      leg["action"],
            "strike":      float(leg["strike"]),
            "option_type": leg["option_type"],
            "lots":        int(leg.get("lots") or 1),
            "lot_size":    int(leg.get("lot_size") or 1),
            "fill_price":  float(leg.get("suggested_price") or 0.0),
        })
    return out


def _net_credit_at_entry(legs: Sequence[dict]) -> float:
    """Total credit (rupees) implied by the suggested fill prices."""
    total = 0.0
    for leg in legs:
        qty = int(leg["lots"]) * int(leg["lot_size"])
        sign = 1.0 if leg["action"] == "SELL" else -1.0
        total += sign * float(leg["fill_price"]) * qty
    return total


# ---------------------------------------------------------------------------
# Single-suggestion walk
# ---------------------------------------------------------------------------

def _simulate_suggestion(
    db: SQLServerConnection,
    suggestion: dict,
    sug_legs: Sequence[dict],
) -> Optional[dict]:
    """Walk one suggestion forward day-by-day until exit or expiry.

    Returns a result dict, or None when the suggestion can't be simulated
    (e.g. invalid strategy, missing data on the very first day).
    """
    fo = FoEodRepo(db)

    strategy = suggestion.get("strategy") or ""
    if strategy in ("", "NONE"):
        return None

    underlying = sug_legs[0]["symbol"]
    expiry = sug_legs[0]["expiry_date"]
    if isinstance(expiry, datetime):
        expiry = expiry.date()
    generated_on = suggestion.get("generated_on")
    if isinstance(generated_on, datetime):
        generated_on = generated_on.date()
    if not generated_on:
        return None

    legs_eng = _legs_for_engine(sug_legs)
    entry_credit = _net_credit_at_entry(legs_eng)

    # Suggestion-level economics (already stored at generation time)
    max_profit_rs = float(suggestion.get("max_profit") or 0.0)
    max_loss_rs   = float(suggestion.get("max_loss") or 0.0)

    # Simulation walks from the day AFTER suggestion generation through expiry.
    # (entry day is fill day; exit decisions begin from next session.)
    sim_date = generated_on + timedelta(days=1)
    final = {
        "suggestion_id": suggestion["suggestion_id"],
        "strategy":      strategy,
        "underlying":    underlying,
        "expiry":        expiry,
        "generated_on":  generated_on,
        "exit_date":     None,
        "exit_decision": "NO_DATA",
        "days_held":     0,
        "exit_pnl":      0.0,
        "max_profit_seen": 0.0,
        "max_loss_seen":   0.0,
        "entry_credit":  entry_credit,
        "max_profit_rs": max_profit_rs,
        "max_loss_rs":   max_loss_rs,
    }

    days_with_data = 0
    while sim_date <= expiry:
        chain = fo.get_chain(underlying, sim_date, expiry)
        if not chain:
            sim_date += timedelta(days=1)
            continue
        days_with_data += 1
        chain_lookup = _build_chain_lookup(chain)

        # Build current_chain in the dict shape evaluate_exit expects
        current_chain = [
            {"strike": k[0], "option_type": k[1], "mid_price": v}
            for k, v in chain_lookup.items()
        ]

        # MTM
        current_value = 0.0
        for leg in legs_eng:
            mid = chain_lookup.get((float(leg["strike"]), leg["option_type"]), 0.0)
            qty = int(leg["lots"]) * int(leg["lot_size"])
            sign = -1.0 if leg["action"] == "SELL" else 1.0
            current_value += sign * mid * qty
        current_pnl = entry_credit + current_value

        if current_pnl > final["max_profit_seen"]:
            final["max_profit_seen"] = current_pnl
        if current_pnl < final["max_loss_seen"]:
            final["max_loss_seen"] = current_pnl

        dte = days_between(sim_date, expiry)
        decision = evaluate_exit(
            trade_id=suggestion["suggestion_id"],
            legs=legs_eng,
            current_chain=current_chain,
            entry_net_credit=entry_credit,
            max_profit_rs=max_profit_rs,
            max_loss_rs=max_loss_rs,
            sl_level_per_share=None,
            days_to_expiry=dte,
            strategy=strategy,
            as_of=datetime.combine(sim_date, datetime.min.time()),
        )

        if decision.decision != "HOLD":
            final.update({
                "exit_date":     sim_date,
                "exit_decision": decision.decision,
                "days_held":     (sim_date - generated_on).days,
                "exit_pnl":      current_pnl,
            })
            return final

        sim_date += timedelta(days=1)

    # Reached expiry without an explicit exit — close at last available MTM.
    if days_with_data > 0:
        final.update({
            "exit_date":     expiry,
            "exit_decision": "EXPIRE_NO_TRIGGER",
            "days_held":     (expiry - generated_on).days,
            "exit_pnl":      current_pnl,  # noqa: F821 — set in loop on last iteration
        })
        return final

    return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _aggregate(results: List[dict]) -> Dict[str, dict]:
    """Group by strategy → win rate, avg P&L, etc."""
    by_strategy: Dict[str, List[dict]] = defaultdict(list)
    for r in results:
        by_strategy[r["strategy"]].append(r)

    summary: Dict[str, dict] = {}
    for strategy, rows in by_strategy.items():
        n = len(rows)
        wins = sum(1 for r in rows if r["exit_pnl"] > 0)
        pnls = [r["exit_pnl"] for r in rows]
        decisions: Dict[str, int] = defaultdict(int)
        for r in rows:
            decisions[r["exit_decision"]] += 1
        summary[strategy] = {
            "trades":      n,
            "win_rate":    wins / n if n else 0.0,
            "avg_pnl":     sum(pnls) / n if n else 0.0,
            "total_pnl":   sum(pnls),
            "best":        max(pnls) if pnls else 0.0,
            "worst":       min(pnls) if pnls else 0.0,
            "avg_days":    sum(r["days_held"] for r in rows) / n if n else 0.0,
            "exits":       dict(decisions),
        }
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_backtest(
    start: date,
    end: date,
    output_dir: Path | None = None,
) -> Dict[str, dict]:
    """Run the backtest over [start, end] (inclusive). Returns per-strategy summary."""
    output_dir = output_dir or Path.cwd()

    db = SQLServerConnection()
    sug = SuggestionRepo(db)

    # Pull all candidate suggestions in window
    rows = db.fetch_all(
        "SELECT * FROM options_suggestions "
        "WHERE generated_on >= ? AND generated_on <= ? "
        "AND strategy <> 'NONE' AND (status IS NULL OR status <> 'NO_SUGGESTION') "
        "ORDER BY generated_on ASC",
        [start, end],
    )
    logger.info("Backtest window %s..%s — %d suggestions", start, end, len(rows))

    results: List[dict] = []
    for suggestion in rows:
        legs = sug.legs(suggestion["suggestion_id"])
        if not legs:
            continue
        out = _simulate_suggestion(db, suggestion, legs)
        if out:
            results.append(out)

    db.close()

    if not results:
        logger.warning("No simulatable suggestions in window")
        return {}

    # Write per-trade CSV
    ts = now_ist().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"backtest_results_{ts}.csv"
    fields = [
        "suggestion_id", "strategy", "underlying", "expiry", "generated_on",
        "exit_date", "exit_decision", "days_held", "exit_pnl",
        "max_profit_seen", "max_loss_seen",
        "entry_credit", "max_profit_rs", "max_loss_rs",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k) for k in fields})
    logger.info("Per-trade CSV: %s", csv_path)

    summary = _aggregate(results)
    return summary


def _print_summary(summary: Dict[str, dict]) -> None:
    if not summary:
        print("No results.")
        return
    print()
    print(f"{'Strategy':<22} {'N':>5} {'WinRate':>8} {'AvgPnL':>10} {'TotalPnL':>12} "
          f"{'Best':>10} {'Worst':>10} {'AvgDays':>8}  Exits")
    print("-" * 110)
    for strat, s in sorted(summary.items()):
        exits_str = ", ".join(f"{k}:{v}" for k, v in sorted(s["exits"].items()))
        print(
            f"{strat:<22} {s['trades']:>5} "
            f"{s['win_rate']*100:>7.1f}% {s['avg_pnl']:>10.0f} {s['total_pnl']:>12.0f} "
            f"{s['best']:>10.0f} {s['worst']:>10.0f} {s['avg_days']:>8.1f}  {exits_str}"
        )
    print()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest suggestion exits over a date range")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end",   required=True, help="YYYY-MM-DD")
    parser.add_argument("--output-dir", default=".", help="CSV output directory")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end   = datetime.strptime(args.end,   "%Y-%m-%d").date()
    summary = run_backtest(start, end, Path(args.output_dir))
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
