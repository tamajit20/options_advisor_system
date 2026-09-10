# Future Enhancement Scopes — Options Advisor System

All known gaps, deferred items, and improvement ideas in one place.  
Pick up from here in future development sessions.

> **Convention:** Every entry below is paired with a `@pytest.mark.future` skipped test stub in `tests/`. When an entry is implemented, the skip is removed AND the entry is deleted from this doc. See `tests/README.md` and `.github/copilot-instructions.md` for the full convention.

**Last pruned:** 2026-09-10 — removed items already shipped (strangle ±1×EM, jade credit gate, LONG_STRANGLE routing, calendar in mid-IV, dynamic lots, OI-change PCR, VIX-spike veto on credit, PCR regen poll, LiveRiskMonitor, slippage/charges in simulator, strategy-selector / suggestion / trade-executor / dashboard-route tests).

---

## 🔴 Risk & Monitoring (Genuine Loss Risk)

### Overnight gap risk ⚠️
**Issue:** The 1.5× credit SL is intraday only. A surprise overnight gap (RBI decision, global shock, earnings) can put short options deep ITM before the exit engine runs. This is inherent to short-premium strategies but can be partially mitigated:
1. **Event-aware forced early exit** — if `event_repo.has_high_impact(tomorrow, tomorrow+1)` is True, flag the trade for evening close (add `PRE_EVENT_EXIT` alert in `lifecycle/exit_orchestrator.py`)
2. **Gap-buffer SL** — widen SL to 2.5× credit when VIX is rising AND a high-impact event is within 2 days
3. **Reduce lot size on high-event weeks** — position sizing multiplier < 1.0 when event risk is elevated

### LiveRiskMonitor — per-leg sanity check on tick prices
**File:** `lifecycle/live_risk_monitor.py`  
**Issue:** A single fat-finger tick (10× normal price) can fire a spurious SL_TRIGGER. The monitor accepts whatever LTP the WebSocket delivers without sanity-checking against the prior tick or against a band around the leg's prior close.  
**Fix:** Maintain a `prev_ltp` per leg; reject a tick if `abs(ltp - prev_ltp) / prev_ltp > 0.50` (configurable). Also reject ticks where ltp ≤ 0. Log rejections under counter `bad_ticks_skipped`. Add test for fat-finger rejection.  
**Test:** `tests/test_lifecycle/test_live_risk_monitor.py::test_fat_finger_tick_is_rejected`

### Enable daily trade Greeks inside VM uptime
**Issue:** `options_trade_greeks` and `trade_greeks_update` exist, and `engine/greeks_exit.py` can consume them, but the job is **disabled** and scheduled at **21:00 IST** (VM is already off at 15:45). Open-trade Greek drift is therefore not actually refreshed.  
**Fix:** Enable the job and move it into Mon–Fri 08:55–15:45 IST. Do not treat this as a suggestion-gate change.

### Do not harden IV trajectory / OI PCR gates yet
**Files:** `engine/confidence.py`  
**Issue:** These gates are advisory `SOFT_FAIL` by design. An older note said to promote them to hard FAIL after 2–3 weeks.  
**Status (2026-09-10):** Closed-trade review (16 trades, May–Jul 2026) did **not** show a clean win/loss split that would justify blocking. Hardening now would miss opportunities.  
**Fix:** Keep as warnings. Revisit only after a larger closed set that includes both credit and debit, and both quiet and burst tapes. Tests stay skipped until that review says yes.  
**Tests:** `tests/test_engine/test_confidence.py::test_iv_trajectory_gate_hardens_to_fail`, `::test_oi_momentum_gate_hardens_to_fail`

### Trade Action Panel — live spot SL in instruction priority
**Files:** `lifecycle/live_risk_monitor.py`, `dashboard/static/dashboard.js`  
**Issue:** The operator instruction banner (`renderTradeActionPanel`) uses today's `SL_TRIGGER` notification or static tags, not live underlying vs `actual_stop_loss_level` on every tick. Spot can breach before the alert/cache updates, so the panel may still say HOLD while spot SL is already violated.  
**Fix:** Include `spot`, `spot_sl_breached`, and `spot_sl_side` in the MTM SSE payload. Re-run the same priority stack with live spot for strategies that have a stored SL level.  
**Test:** `tests/test_dashboard/test_trade_action_future.py::test_action_panel_uses_live_spot_sl_not_stale_alert`

### Trade Action Panel — leg-level close instructions
**Files:** `dashboard/static/dashboard.js`, optional helper in `engine/exit_engine.py` or `lifecycle/trade_executor.py`  
**Issue:** Instructions say "close entire trade" or "close call spread" but not concrete leg actions like "Buy back NIFTY 26000 PE @ ~₹120 (1 lot)".  
**Fix:** From executed open legs + live `leg_ltps`, render per-leg exit lines.  
**Test:** `tests/test_dashboard/test_trade_action_future.py::test_action_panel_lists_per_leg_exit_with_ltp`

### Trade Action Panel — per-leg intraday SL integration
**Files:** `lifecycle/intraday_monitor.py`, `dashboard/static/dashboard.js`  
**Issue:** `IntradayMonitor` fires per-short-leg `SL_TRIGGER` when premium doubles; the Trade Action Panel does not read that path.  
**Fix:** Feed leg-level breach state into the action panel.  
**Test:** `tests/test_dashboard/test_trade_action_future.py::test_action_panel_surfaces_intraday_leg_sl_breach`

### Trade Action Panel — multi-condition summary
**Files:** `dashboard/static/dashboard.js`  
**Issue:** Priority stack shows one verb only. When multiple levels are active, context is hidden.  
**Fix:** Keep single primary action; add an "Also active:" line listing secondary levels.  
**Test:** `tests/test_dashboard/test_trade_action_future.py::test_action_panel_shows_secondary_active_levels`

### Breach history UI on trade card
**Files:** `dashboard/server.py`, `dashboard/static/dashboard.js`, `database/models.py` (`TradeLevelEventRepo`)  
**Issue:** `options_trade_level_events` logs ENTER/EXIT but there is no dashboard timeline.  
**Fix:** `GET /api/trades/<trade_id>/level-events` and a collapsible timeline on the trade card.  
**Test:** `tests/test_dashboard/test_trade_action_future.py::test_trade_card_renders_level_event_timeline`

### Broker order execution from action panel
**Status:** Explicitly deferred — panel is advisory only.  
**Issue:** One-click "close in Zerodha" would need OAuth, order placement API, partial-fill handling, and audit trail.  
**Fix:** Only after explicit product decision; until then instruction ends at Close Trade + manual broker entry.

---

## 🟡 Strategy & Regime Coverage

### Unused suggestion signals — validate before any implementation
**Do not implement blindly. Do not add as hard FAIL. Go one signal at a time after a documented win/loss check.**

Closed-trade join on 2026-09-10 (16 CLOSED rows, 4 wins / 12 losses) found **no unused signal with a usable edge**:

| Candidate | Already in DB / WS? | Historical result | Allowed next step |
|-----------|---------------------|-------------------|-------------------|
| IV percentile | stored on `options_iv_history` | Tracks IV rank; same cheap-vol loss cluster | Display/warning only after a larger sample |
| FII option OI (call/put long−short) | stored; only FII *futures* net is used | FII was net **buying** options on all 16 trades — no contrast | Wait until FII *writing* days exist in the book |
| Max-pain vs spot | computed, unused | 8 above / 8 below, both 25% wins | Do not use for direction |
| Volume burst z | computed on 5-min chain, unused in gates | Every entry was a quiet tape | Do not gate |
| Smile / 25d-proxy risk reversal | per-strike IV exists | Means almost identical on wins vs losses | Ignore until wing IVs are trusted |

**Rules if we ever pick one up:**
1. Re-run the closed-trade join (need more **credit** trades and mixed FII regimes).
2. If a split appears, ship as **advisory chip / SOFT_FAIL** only.
3. Watch live cards for 2–3 weeks. Only then consider a tilt (prefer structure), never a veto, unless the sample is clearly large enough.
4. Next candidate only after the previous one is accepted or dropped. No batch wiring.

**Test:** `tests/test_engine/test_unused_signals_future.py::test_unused_signals_not_wired_until_validated`

### Same-direction concentration penalty across symbols
**Files:** `lifecycle/suggestion_engine.py`, possibly new `lifecycle/portfolio_concentration.py`
**Issue:** Two BULL_PUT_SPREAD suggestions on NIFTY and BANKNIFTY can both fire. Cross-underlying dedup only collapses identical (expiry_type, strategy) keys.
**Fix:** After all underlyings are evaluated, demote the weaker same-direction suggestion. Preserve strategy isolation (credit vs debit verticals).
**Why deferred:** Requires surfacing Suggestions to the orchestrator before persistence.

### Side-aware SL multiplier
**Issue:** Put-side breach uses the same 1.5× multiplier as call-side. Markets fall faster than they rise.
**Fix:** Add asymmetric multipliers (e.g. 1.5× call-side, 1.25× put-side) **after backtest confirms** asymmetric hit rates. Files: `engine/strategy_selector.py`, `lifecycle/exit_orchestrator.py`.

---

## 🟡 Data Quality

### HV-20 PASS_WARN escalation — silent data gap
**Issue:** The HV-20 gate silently passes with `PASS_WARN` when < 22 days of history exist. For a new underlying with an ongoing data gap this never escalates to FAIL.  
**Fix:** Add a counter; escalate to FAIL after N consecutive `PASS_WARN` days.

---

## 🟢 Simulation / Backtesting

### Time-series replay simulator
**Files:** New `simulation/timeseries_replay.py`
**Issue:** 5-min chain history exists, but no replay harness to quantify how often trajectory gates would have fired, or whether hardening them would have helped or hurt P&L. This is the right tool before promoting any unused signal or SOFT_FAIL gate.
**Fix:** Reconstruct `ChainTrajectory` at past `snapshot_at`, run `engine.confidence.evaluate()`, tabulate gate-firing vs downstream P&L.  
**Test:** `tests/test_simulation/test_simulator.py::test_timeseries_replay_runner_reconstructs_trajectory`

### Full multi-day simulation walkthrough
**Issue:** No test walks a synthetic chain through every trading day of an iron condor's life.  
**Fix:** 14-day synthetic chain fixture; day-by-day P&L + expiry close.  
**Test:** `tests/test_simulation/test_simulator.py::test_full_simulation_walk_to_expiry`

---

## 📋 Discussed but Deferred (User Decision)

| Item | Status |
|---|---|
| Broker-agnostic adapter layer (ZerodhaAdapter / NoOpAdapter) | Discussed only — not implementing yet |
| Telegram / email notification dispatcher | Discussed only — not implementing yet |
| Trade Action Panel — full rule fusion + broker execution | Advisory panel shipped May 2026; remaining UI items → Risk & Monitoring |
| BANKNIFTY/FINNIFTY weekly options (NSE discontinued ~Nov 2024) | No fix needed — they reappear when monthly expiry DTE ≤ 21 |
| VIX ghost rows on non-trading days (cosmetic) | Left as-is by user choice (May 2026) |
| Unused suggestion signals as **hard** filters | Rejected until a larger closed-trade sample (Sep 2026 review) |

---

## References
- Phases 1–4 implemented: `8763410`, `a64d158`, `d99f18c`, `fa2aea3`
- UI enhancements (IV/HV chip, exec order badges, lot validation, confirm buttons): `2539eb4`, `289a41d`, `2cbaa6d`
- Archive / laptop retry / SQL log shrink: `cabf7f0`, `d0eacd6`
- Backtest runner: `python -m simulation.backtest_runner --start YYYY-MM-DD --end YYYY-MM-DD`
