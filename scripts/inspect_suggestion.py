"""Inspect one suggestion's trend inputs."""
import json
import sys
from datetime import timedelta

from database.connection import SQLServerConnection
from database.models import SpotEodRepo
from engine.indicators import adx, trend_sma_periods, _sma_slope_pct
from engine.trend_model import compute_trends, filter_spot_history

sid = sys.argv[1] if len(sys.argv) > 1 else "SUG-20260518-004"

db = SQLServerConnection()
row = db.fetch_one(
    "SELECT suggestion_id, underlying, generated_on, data_date, data_source, "
    "trigger_type, strategy_type, spot_at_generation, conditions_json "
    "FROM options_suggestions WHERE suggestion_id = ?",
    [sid],
)
if not row:
    print("NOT FOUND:", sid)
    db.close()
    raise SystemExit(1)

print("=== SUGGESTION ===")
for k, v in row.items():
    if k != "conditions_json":
        print(f"  {k}: {v}")

cj = row.get("conditions_json")
if cj:
    checks = json.loads(cj) if isinstance(cj, str) else cj
    for c in checks:
        det = c.get("detail") or ""
        if "trend" in (c.get("label") or "").lower() or "Trend" in det:
            print("  TREND CHECK:", c)

sym = row["underlying"]
td = row["data_date"]
sp = SpotEodRepo(db)
hist_f = filter_spot_history(sp.history(sym, td - timedelta(days=120)), td)
print(f"\n=== LAST 10 CLOSES {sym} (as_of {td}) ===")
for r in hist_f[-10:]:
    h, l, c = float(r["high_price"]), float(r["low_price"]), float(r["close_price"])
    flat = (h - l) < 0.01
    print(f"  {r['trade_date']}  close={c:.1f}  range={h-l:.1f}  flat_ohlc={flat}")

closes = [float(r["close_price"]) for r in hist_f]
spot_now = float(row["spot_at_generation"] or closes[-1])
fast_p, slow_p = trend_sma_periods()
if len(closes) >= slow_p:
    sma_f = sum(closes[-fast_p:]) / fast_p
    sma_s = sum(closes[-slow_p:]) / slow_p
    diff = (sma_f - sma_s) / sma_s * 100
    slope = _sma_slope_pct(closes, fast_p)
    adx_v = adx(hist_f, 14)
    eff, struct, sess, ret_pct, ret_tr = compute_trends(
        spot_history=hist_f,
        as_of=td,
        spot_now=spot_now,
        session_bar=None,
        live_mode=row.get("data_source") == "LIVE",
    )
    print("\n=== TREND GATES ===")
    print(f"  SMA{fast_p}-{slow_p} diff: {diff:+.2f}%  (bearish needs < -0.5%)")
    print(f"  SMA{fast_p} 5d slope: {slope:+.3f}%  (bearish needs < -0.05%)")
    print(f"  ADX-14: {adx_v:.2f}  (needs >= 18)")
    print(f"  structural: {struct}")
    print(f"  session (if live): {sess}")
    print(f"  return_pct: {ret_pct}  return_trend: {ret_tr}")
    print(f"  effective: {eff}")
    reasons = []
    if adx_v is None or adx_v < 18:
        reasons.append("ADX too low")
    if diff > -0.5:
        reasons.append(f"SMA{fast_p} not 0.5% below SMA{slow_p}")
    if slope is None or slope >= -0.05:
        reasons.append(f"SMA{fast_p} slope not falling")
    if ret_tr != "BEARISH":
        reasons.append("5d/10d return not <= -1.5%")
    print(f"  why not BEARISH (structural): {reasons or ['return override may still apply']}")
else:
    print(f"Only {len(hist_f)} rows (<{slow_p}) -> forced SIDEWAYS")

db.close()
