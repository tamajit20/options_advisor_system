# Future Enhancement Scopes — Options Advisor System

All known gaps, deferred items, and improvement ideas in one place.  
Pick up from here in future development sessions.

> **Convention:** Every entry below is paired with a `@pytest.mark.future` skipped test stub in `tests/`. When an entry is implemented, the skip is removed AND the entry is deleted from this doc. See `tests/README.md` and `.github/copilot-instructions.md` for the full convention.

---

## 🔴 Engine Correctness (Fix These First)

### LONG_STRANGLE strikes too close to ATM
**File:** `engine/leg_builder.py`  
**Issue:** Strikes are built at `±0.5 × EM` (inside the expected move). Near-the-money strikes = high cost, less edge.  
**Fix:** Change to `±1.0 × EM` so strikes sit at the boundary of the expected move.

### JADE_LIZARD — no net-credit ≥ call-spread-width validation
**File:** `engine/leg_builder.py` or `engine/strategy_selector.py`  
**Issue:** A valid Jade Lizard requires `net_credit >= call_spread_width`. If premium is thin this is silently violated, leaving undefined upside risk on the naked short put.  
**Fix:** Add gate: `if net_credit < call_spread_width: raise StrategyVeto`.

### LONG_STRANGLE strategy is dead code — never triggered
**Files:** `engine/strategy_selector.py`, `engine/leg_builder.py`  
**Issue:** `leg_builder.py` has `build_long_strangle()` but `strategy_selector.py` never routes to it.  
**Fix:** Either implement trigger conditions (low IV rank + expected breakout / event-driven) or remove the dead function.

---

## 🔴 Risk & Monitoring (Genuine Loss Risk)

### Overnight gap risk ⚠️
**Issue:** The 1.5× credit SL is intraday only. A surprise overnight gap (RBI decision, global shock, earnings) can put short options deep ITM before the exit engine runs. This is inherent to short-premium strategies but can be partially mitigated:
1. **Event-aware forced early exit** — if `event_repo.has_high_impact(tomorrow, tomorrow+1)` is True, flag the trade for evening close (add `PRE_EVENT_EXIT` alert in `lifecycle/exit_orchestrator.py`)
2. **Gap-buffer SL** — widen SL to 2.5× credit when VIX is rising AND a high-impact event is within 2 days
3. **Reduce lot size on high-event weeks** — position sizing multiplier < 1.0 when event risk is elevated

### Intraday SL monitoring
**Files:** New `lifecycle/sl_monitor.py`, `scheduler/scheduler.py`  
**Issue:** No automated intraday monitoring. SL breaches can only be caught manually.  
**Fix:** Add intraday scheduler job (09:30–15:00 IST, every 30 min) that fetches live NSE option chain JSON and alerts if MTM loss ≥ premium SL or spot crosses the SL level. NSE option chain is publicly accessible without auth (no broker API needed).

### No VIX regime / slope filter on Iron Condor entry
**Files:** `engine/indicators.py`, `engine/strategy_selector.py`  
**Issue:** High IV Rank during a VIX spike ≠ safe to sell premium — the market is pricing in *continued* large moves. Engine currently treats all high-IV-rank environments the same.  
**Fix:** Add VIX rate-of-change check: skip IC/Butterfly suggestion if VIX has risen >20% over the last 3 trading days.

### Greek drift tracking on open trades
**Issue:** Greeks (vega/delta/theta) are computed at suggestion time but never tracked on open trades. A trade at 50% profit target but with exploding vega is risky to hold.  
**Fix:** Add daily Greek recomputation stored against the trade record in `options_trades`.

---

## 🟡 Strategy & Regime Coverage

### Mid-IV (30–50) sideways regime — missed trades
**Issue:** Mid-IV sideways currently results in a `StrategyVeto` ("no actionable edge"). Calendar spreads or short iron flies with tight wings could work here.  
**Fix:** Evaluate once backtest data shows how often this regime occurs. If frequent, add Calendar Spread build to `leg_builder.py` and route to it from `strategy_selector.py`.

### Side-aware SL multiplier
**Issue:** Put-side breach uses the same 1.5× multiplier as call-side. Markets fall faster than they rise — put-spread breaches tend to be more violent.  
**Fix:** Add asymmetric multipliers (e.g. 1.5× call-side, 1.25× put-side) after backtest confirms asymmetric hit rates. Files: `engine/strategy_selector.py`, `lifecycle/exit_orchestrator.py`.

---

## 🟡 Position Sizing

### Lots hardcoded to 1
**Files:** `lifecycle/suggestion_engine.py` (lines 319, 369)  
**Issue:** `lots=1` is hardcoded. Optimal sizing = `risk_per_trade = capital × 0.02 / max_loss_per_lot`.  
**Blocked by:** No capital input or broker margin info in the system yet.  
**Fix:** Add a `trading_capital` config key, compute lots dynamically in the suggestion engine.

---

## 🟡 Data Quality

### OI change (delta) not tracked
**File:** `engine/indicators.py`  
**Issue:** Uses raw OI level from the chain. Day-over-day OI change per strike is a better conviction signal (OI building = real positioning, OI shedding = unwinding).  
**Fix:** Track prior-day OI in `options_fo_eod` and compute delta in `build_indicators()`.

### HV-20 PASS_WARN escalation — silent data gap
**Issue:** The HV-20 gate silently passes with `PASS_WARN` when < 22 days of history exist. For a new underlying with an ongoing data gap this never escalates to FAIL.  
**Fix:** Add a counter to IV history repo; escalate to FAIL after N consecutive `PASS_WARN` days.

### VIX live fallback stamps wrong trade_date on non-trading days
**File:** `downloader/vix.py` — `_fetch_live_vix()`  
**Issue:** Uses `today_ist()` as `trade_date`, creating ghost rows on holidays/weekends with stale OHLC (e.g. May 1 holiday, May 2 Saturday both got Apr 30's data with wrong dates).  
**Fix:** Before inserting, check if `today_ist()` is a trading day. If not, skip the live fetch.

---

## 🟢 Simulation / Backtesting

### Simulator ignores bid/ask slippage
**File:** `simulation/simulator.py`  
**Issue:** Fills assumed at mid-price. Real fills on far-OTM strikes can be 2–5% worse due to wide spreads and low liquidity. Makes simulated P&L look better than reality.  
**Fix:** Add configurable `slippage_bps` parameter (default 0; suggest 50–100 bps for realistic runs). Apply as `fill_price = mid ± (mid × slippage_bps / 10000)`.

---

## 🟢 Code Quality & Testing

### Strategy selector unit tests — critical gap
**Issue:** The 11-strategy decision tree has many branches. A silent regression here is catastrophic — a wrong strategy gets suggested with full confidence.  
**Fix:** Add `tests/test_strategy_selector.py` covering all IV-regime × trend × PCR combinations.

### Companion BPS/BCS strike optimization
**Issue:** Companion BPS/BCS spreads reuse IC strike selection (which optimises for full-range neutrality). A standalone BPS/BCS may prefer strikes closer to the money.  
**Fix:** Add independent strike selection for companions when they are the primary strategy.

### Suggestion engine integration tests
**Issue:** `lifecycle/suggestion_engine.py` (~480 lines) is the central orchestrator wiring downloader → indicators → strategy selector → leg builder → confidence → DB. Currently zero direct test coverage; only the underlying engine modules are unit-tested.  
**Fix:** Build a fake-DB harness covering: (a) happy-path SUG-* row insert with legs, (b) NO_SUGGESTION when confidence below threshold, (c) deduplication via `has_suggestion_for`, (d) `expire_stale_pending` is called before fresh insert.  
**Tests:** `tests/test_lifecycle/test_suggestion_engine_future.py` (4 stubs)

### Trade executor unit tests
**Issue:** `lifecycle/trade_executor.py` records actual fills and computes actual_max_profit/loss. Currently no direct tests.  
**Fix:** Mock TradeRepo + verify TRD-* row written with correct economics from fill prices.  
**Test:** `tests/test_lifecycle/test_suggestion_engine_future.py::test_trade_executor_records_fill_prices`

### Dashboard route coverage — close/supplement/config endpoints
**Issue:** Phase 4 added smoke + helper tests for `dashboard/server.py`, but the
close-trade, supplement-trade, and config GET/PATCH routes are still stubbed.  
**Fix:** Wire the remaining POST/PATCH routes to test fixtures and assert the
DB writes happen as expected.  
**Tests:** `tests/test_dashboard/test_server.py::test_close_trade_persists_exit_fills`,
`::test_supplement_adds_remaining_legs`, `::test_config_get_and_patch`

### Full multi-day simulation walkthrough
**Issue:** Phase 4 covers `_classify_day1` and `_compute_day_pnl`, but no test
walks a synthetic chain through every trading day of an iron condor's life.  
**Fix:** Build a 14-day synthetic chain fixture and assert day-by-day P&L
progression + correct expiry-day close.  
**Test:** `tests/test_simulation/test_simulator.py::test_full_simulation_walk_to_expiry`

### Simulation: include estimated charges in net P&L
**Issue:** `update_simulation` hardcodes `sim_charges=0.0`, so `sim_net_pnl`
matches `sim_final_pnl`. Real-world net P&L is materially lower after STT,
brokerage, and exchange fees.  
**Fix:** Call `engine.charges.estimate_charges` on the simulated fills and
subtract from gross P&L when writing the summary row.  
**Test:** `tests/test_simulation/test_simulator.py::test_simulation_includes_charges_in_net_pnl`

---

## 📋 Discussed but Deferred (User Decision)

| Item | Status |
|---|---|
| Broker-agnostic adapter layer (ZerodhaAdapter / NoOpAdapter) | Discussed only — not implementing yet |
| Telegram / email notification dispatcher | Discussed only — not implementing yet |
| BANKNIFTY/FINNIFTY weekly options (NSE discontinued ~Nov 2024) | No fix needed — they reappear when monthly expiry DTE ≤ 21 |
| VIX ghost rows on non-trading days (cosmetic) | Left as-is by user choice (May 2026) |

---

## References
- Phases 1–4 implemented: `8763410`, `a64d158`, `d99f18c`, `fa2aea3`
- UI enhancements (IV/HV chip, exec order badges, lot validation, confirm buttons): `2539eb4`, `289a41d`, `2cbaa6d`
- Backtest runner: `python -m simulation.backtest_runner --start YYYY-MM-DD --end YYYY-MM-DD`
- Backtest data window (as of May 2026): chain data available 2026-02-06 to 2026-04-30
