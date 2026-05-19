"""One-off: diagnose trend classification vs DB spot history and suggestions."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database.connection import SQLServerConnection
from database.models import SpotEodRepo
from engine.indicators import adx, trend, _sma20_slope_pct
from config import STRATEGY_CONFIG


def _trend_components(spot_history):
    closes = [float(r["close_price"]) for r in spot_history]
    if len(closes) < 50:
        return {"n": len(closes), "trend": "SIDEWAYS", "reason": "<50 rows"}
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    diff_pct = (sma20 - sma50) / sma50 * 100.0 if sma50 > 0 else 0.0
    slope = _sma20_slope_pct(closes)
    adx_val = adx(spot_history, 14)
    sma_min = STRATEGY_CONFIG.get("trend_sma_diff_pct", 0.5)
    slope_min = STRATEGY_CONFIG.get("trend_slope_min_pct", 0.05)
    adx_min = STRATEGY_CONFIG.get("trend_adx_min", 20.0)
    t = trend(spot_history)
    reasons = []
    if adx_val is None:
        reasons.append("adx=None")
    elif adx_val < adx_min:
        reasons.append(f"adx={adx_val:.1f}<{adx_min}")
    if abs(diff_pct) <= sma_min:
        reasons.append(f"|sma_diff|={abs(diff_pct):.3f}%<={sma_min}%")
    elif diff_pct > 0 and (slope is None or slope <= slope_min):
        reasons.append(f"slope={slope} (need >{slope_min})")
    elif diff_pct < 0 and (slope is None or slope >= -slope_min):
        reasons.append(f"slope={slope} (need <-{slope_min})")
    return {
        "n": len(closes),
        "trend": t,
        "sma_diff_pct": round(diff_pct, 4),
        "sma20_slope_pct": round(slope, 4) if slope is not None else None,
        "adx_14": round(adx_val, 2) if adx_val is not None else None,
        "thresholds": {"sma_min": sma_min, "slope_min": slope_min, "adx_min": adx_min},
        "why_sideways": reasons or ["all gates passed but still SIDEWAYS?"],
    }


def main():
    db = SQLServerConnection()
    sp = SpotEodRepo(db)
    symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY"]

    print("=== options_spot_eod row counts ===")
    for sym in symbols:
        row = db.fetch_one(
            "SELECT COUNT(*) AS cnt, MIN(trade_date) AS mn, MAX(trade_date) AS mx "
            "FROM options_spot_eod WHERE symbol = ?",
            [sym],
        )
        print(f"  {sym}: {row['cnt']} rows  {row['mn']} .. {row['mx']}")

    print("\n=== Spot data quality (last 5 rows NIFTY) ===")
    recent = db.fetch_all(
        "SELECT TOP 5 trade_date, open_price, high_price, low_price, close_price "
        "FROM options_spot_eod WHERE symbol = 'NIFTY' ORDER BY trade_date DESC"
    )
    for r in recent:
        h, l, c = float(r["high_price"]), float(r["low_price"]), float(r["close_price"])
        flat = h == l == c
        print(f"  {r['trade_date']} close={c} H-L={h-l:.1f} flat_OHLC={flat}")

    dup = db.fetch_one(
        "SELECT COUNT(*) - COUNT(DISTINCT trade_date) AS dups "
        "FROM options_spot_eod WHERE symbol = 'NIFTY'"
    )
    print(f"  NIFTY duplicate trade_dates: {dup['dups']}")

    print("\n=== Trend as-of latest bhav (120d history like engine) ===")
    latest = sp.latest("NIFTY")
    if latest:
        td = latest["trade_date"]
        since = td - timedelta(days=120)
        hist = sp.history("NIFTY", since)
        comp = _trend_components(hist)
        print(f"  as_of={td}  spot_close={latest['close_price']}")
        print(f"  {json.dumps(comp, indent=2)}")

    print("\n=== Rolling trend last 30 trading days (NIFTY) ===")
    all_dates = [
        r["trade_date"]
        for r in db.fetch_all(
            "SELECT trade_date FROM options_spot_eod WHERE symbol = 'NIFTY' "
            "ORDER BY trade_date DESC OFFSET 0 ROWS FETCH NEXT 30 ROWS ONLY"
        )
    ]
    dist = Counter()
    for td in sorted(all_dates):
        since = td - timedelta(days=120)
        hist = sp.history("NIFTY", since)
        t = trend(hist)
        dist[t] += 1
    print(f"  distribution: {dict(dist)}")

    print("\n=== Suggestions: trend from conditions_json (last 200) ===")
    sug_rows = db.fetch_all(
        "SELECT TOP 200 suggestion_id, underlying, generated_on, conditions_json "
        "FROM options_suggestions ORDER BY generated_on DESC"
    )
    trend_from_checks = Counter()
    parsed = 0
    for r in sug_rows:
        cj = r.get("conditions_json")
        if not cj:
            continue
        try:
            checks = json.loads(cj) if isinstance(cj, str) else cj
        except json.JSONDecodeError:
            continue
        if not isinstance(checks, list):
            continue
        for c in checks:
            lbl = (c.get("label") or "")
            if "trend" in lbl.lower():
                m = re.search(r"Trend:\s*(\w+)", c.get("detail") or c.get("message") or "", re.I)
                if m:
                    trend_from_checks[m.group(1).upper()] += 1
                    parsed += 1
                break

    print(f"  parsed {parsed} suggestions with Trend check")
    print(f"  trends: {dict(trend_from_checks)}")

    print("\n=== Strategy mix (last 200 suggestions) ===")
    strat = db.fetch_all(
        "SELECT TOP 200 strategy_type FROM options_suggestions ORDER BY generated_on DESC"
    )
    print(f"  {dict(Counter(r['strategy_type'] for r in strat))}")

    db.close()


if __name__ == "__main__":
    main()
