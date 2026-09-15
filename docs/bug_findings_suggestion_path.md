# Bug findings: suggestion_engine → confidence → strategy_selector → leg_builder

Date: 2026-09-13  
Last review: 2026-09-15 (third pass — more findings after developer fixes)  
Scope: failure modes only (not design opinions).

## Fix status summary

| # | Issue | Status | Why |
|---|---|---|---|
| **1** | Missing VIX → `TypeError` in `_explain` → underlying dropped | **FIXED** | Pure tech crash (`None:.1f`). Guard in `_explain`. |
| **2** | Collapsed iron-condor wings (width 0 / max loss 0) | **FIXED** | Hard `StrategyVeto` when credit `spread_width <= 0` (invalid structure / existing gate hole). |
| **3** | `strategy_min_soft_pass` counts `"PASS"` only, not `.passed` | **NOT FIXED** | Intentional gate semantics (existing test). Deferred. |
| **4** | Missing PCR → `TypeError` in `select_strategy` | **FIXED** | Same class as #1. Neutral PCR `1.0` when `None` (getattr default was dead). |
| **5** | Live IV-traj nudge vs writing sell-min desync | **FIXED** | Shared `effective_iv_rank_for_regime()` for pick + regime gates. No threshold changes. |
| **6** | Credit multi-short PoP overstated (delta avg vs BE range) | **NOT FIXED** | Changes PoP/edge methodology and ranking — business/math, not a crash. Deferred. |
| **7** | Calendar remap reuses candidate-expiry `atm_iv` with near DTE | **FIXED** | Data-flow wiring: recompute ATM IV (+ OI rows) for near expiry after remap. |
| **8** | IC/IB companions skip capital sizing + max-loss cap | **FIXED** | Wiring: companions use `_assemble_sized_suggestion` (same path as primary). Concentration dedup left unchanged (product). |
| **9** | EOD `atm_iv=0` → debit PoP collapses to **100%** | **FIXED** | EOD skip when `atm_iv<=0`; `estimate_pop` returns 0 for debit/range when IV/DTE/spot invalid. |
| **10** | Calendar near-remap keeps candidate `expiry_type` | **FIXED** | Retag via `_expiry_type_label(near, all_expiries)` after remap. |
| **11** | `_is_monthly_expiry` misses holiday-shifted monthlies | **FIXED** | Last F&O expiry in calendar month from catalogue (no Thursday requirement). |
| **12** | Jade `spread_width(..., "JADE_LIZARD")` uses put **strike** (~23k) for edge grade | **FIXED** | Grade + credit-to-width veto both use call-wing width. |
| **13** | `mid_price` ignores `last_price` | **FIXED** | Fallback order: settle → close → last. |
| **14** | Calendar allows mismatched near/far strikes (silent diagonal) | **FIXED** | Require shared strike; `ValueError` when near/far ATMs cannot align. |

---

## Bug 1 — Missing VIX: confidence passes, assemble crashes, underlying silently dropped

**Status: FIXED (2026-09-15)** — tech only.

### What was fixed
`_explain` no longer formats `vix_close` when it is `None`; plain English shows `VIX n/a` instead of raising `TypeError`.

### What was not changed
- Confidence still `PASS_WARN` on missing VIX (no gate tightening).
- Outer `suggestion_engine` loop still logs + `continue` on unexpected exceptions (no new `NoSuggestion` path).

### Severity (original)
Hard failure. Entire underlying evaluation dies with `TypeError`. No suggestion and no `NoSuggestion` persisted.

### Trace

1. **`engine/confidence.py`** — `vix_close is None` → `PASS_WARN` (not a fail). Soft tally can still yield `all_passed=True`.

2. **`engine/strategy_selector.py` → `_explain`** — *(was)* `VIX {indicators.vix_close:.1f}` → TypeError; *(now)* `VIX n/a`.

3. **`lifecycle/suggestion_engine.py`** — outer loop still swallows unexpected exceptions.

---

## Bug 2 — Collapsed iron-condor wings: zero width, zero max loss, veto skipped

**Status: FIXED (2026-09-15)** — tech only (invalid structure / existing gate hole).

### What was fixed
Credit strategies with `spread_width <= 0` (same-strike / collapsed wings) raise `StrategyVeto` before the credit-to-width ratio check. No change to EM or wing multipliers.

### What was not changed
Wing aggressiveness, sizing when max_loss > 0, or which strategies are eligible.

### Severity (original)
Hard logic failure. Invalid structure can be assembled and sized as if risk were defined.

### Trace
`closest_strike` can map long = short; credit-to-width veto skipped when width 0; sizing keeps 1 lot when max_loss 0.

---

## Bug 3 — `strategy_min_soft_pass` counts only status `"PASS"`, not confidence “passed”

**Status: NOT FIXED** — deferred (intentional semantics / card volume).

### Why not fixed
Counting `c.passed` would alter who gets cards. Covered by `test_long_call_pass_warn_does_not_count_as_soft_pass`.

---

## Bug 4 — Missing PCR: confidence passes, `select_strategy` crashes, underlying silently dropped

**Status: FIXED (2026-09-15)** — tech only.

### What was fixed
`select_strategy` treats `pcr is None` as neutral `1.0` (no strong bullish/bearish PCR). Prevents `None < float` TypeError.

### What was not changed
Confidence still `PASS_WARN` on missing PCR.

### Trace (original)
`pcr = getattr(indicators, "pcr", 1.0)` returned `None` when the field existed; `pcr < pcr_bull` crashed.

---

## Bug 5 — Live IV-trajectory nudge vs writing sell-min desync

**Status: FIXED (2026-09-15)** — tech desync only.

### What was fixed
Extracted `effective_iv_rank_for_regime()`; used by `select_strategy` and by `assemble_suggestion` regime gates (`strategy_iv_premium_sell_min` / buy caps / long-vol gate). Same nudge, same thresholds.

### What was not changed
Nudge thresholds and sell-min map values.

### Trace (original)
Selection nudged local `iv_rank` into writing; assemble still saw mid-zone raw rank → sell-min skipped.

---

## Bug 6 — Credit multi-short PoP overstated (avg short-delta vs BE-range probability)

**Status: NOT FIXED** — deferred (methodology / ranking).

### Why not fixed
Switching credits to BE-range PoP changes persisted PoP and edge_score. Not a TypeError; product/math choice. Debit/calendar already use range math by design comments in `estimate_pop`.

---

## Bug 7 — Calendar remap keeps candidate-expiry `atm_iv` (and OI deltas) with near DTE

**Status: FIXED (2026-09-15)** — data-flow wiring.

### What was fixed
After calendar near/far remap, recompute ATM IV (live or EOD IV rows) and rebuild live OI change/abs rows for the **near** expiry before `build_indicators` / confidence / assemble.

### What was not changed
Calendar DTE bands or strategy pick rules.

---

## Bug 8 — IC/IB companions inherit primary lots; skip capital sizing + max-loss cap

**Status: FIXED (2026-09-15)** — wiring only.

### What was fixed
Companions call `_assemble_sized_suggestion` (1-lot dry-run + `max_loss_pct_of_capital`) instead of `assemble_suggestion(..., lots=primary_lots)`.

### What was not changed
Cross-underlying concentration / strategy dedup for companions (product call).

---

## Bug 9 — EOD `atm_iv=0` → debit PoP becomes 100%

**Status: FIXED (2026-09-15)** — tech only. EOD skips `atm_iv<=0`; `estimate_pop` returns 0 for debit/range on invalid IV.

### Severity
Wrong persisted economics. Live path skips when `atm_iv <= 0`; EOD only requires an IV row and accepts `0.0`.

### Trace

1. **`suggestion_engine` EOD** (`~680`): `atm_iv = float(iv_for_expiry[0].get("atm_iv") or 0.0)` — no `<= 0` continue.
2. **`leg_builder._prob_below`**: `vol <= 0` → returns `0.5` (“no signal”).
3. Debit two-sided PoP: `p_above + p_below` each 0.5 → **100%**. Credit path with Δ=0 → also ~100%.

### Proof

```text
LONG_STRADDLE estimate_pop(..., atm_iv=0.0)  → 100.0
LONG_STRADDLE estimate_pop(..., atm_iv=0.15) → ~52
```

`expected_move=0` blocks EM builders, but `LONG_STRADDLE` / ATM debit structures still assemble with fake PoP/edge.

### Fix direction
- EOD: `continue` / veto when `atm_iv <= 0` (match live), **and/or**
- `estimate_pop`: refuse degenerate vol (return 50 or raise) instead of summing two 0.5s into 100.

---

## Bug 10 — Calendar near-remap keeps candidate `expiry_type`

**Status: FIXED (2026-09-15)** — retag `use_expiry_type` from near date + catalogue after remap.

### Severity
Wrong persistence / dedup / expire keys. Near expiry date is remapped; `expiry_type` stays the loop tag (`"Monthly"`).

### Trace
`_assemble_kw` still sets `expiry_type=expiry_type` from the candidate loop while `expiry=use_expiry` is the near (often weekly) date. `has_suggestion_for`, `expire_stale_pending`, and cross-underlying dedup key on `(expiry_type, strategy)`.

### Proof sketch

```text
Monthly candidate → CALENDAR_SPREAD
near_expiry = weekly (≠ monthly)
Suggestion.expiry_date = weekly
Suggestion.expiry_type = "Monthly"   # unchanged
```

### Fix direction
Re-tag `expiry_type` from `_is_monthly_expiry(use_expiry)` (after fixing #11), or from the resolved near leg’s true bucket.

---

## Bug 11 — `_is_monthly_expiry` misses holiday-shifted monthlies

**Status: FIXED (2026-09-15)** — last F&O expiry in month from catalogue (no Thursday requirement).

### Severity
Wrong Weekly/Monthly bucket → wrong pick set and dedup keys.

### Trace
`suggestion_engine._is_monthly_expiry` requires `weekday() == 3` (Thursday). NSE monthlies that shift to Wednesday when Thursday is a holiday are tagged Weekly.

### Proof

```text
_is_monthly_expiry(date(2024, 11, 27)) → False  # Wed monthly
_is_monthly_expiry(date(2026, 5, 28))  → True   # last Thu
```

### Fix direction
Last F&O expiry of the month (or holiday calendar), not “must be Thursday”.

---

## Bug 12 — Jade edge/grade width uses naked-put **strike**

**Status: FIXED (2026-09-15)** — Jade grade + CW veto both use call-wing width.

### Severity
Systematically crushed `credit_grade` / `edge_score` for Jade vs peers. Veto path correctly uses call-wing-only width; grade path does not.

### Trace
`spread_width(legs, "JADE_LIZARD")` = `max(call_width, short_put_strike)`. Docstring claims this equals `max_loss + credit`, but feeding a ~23 000 “width” into a ratio designed for 50–200 pt spreads makes every Jade look ~0% credit/width.

### Proof

```text
JL credit 50, call width 100, short put 22800
spread_width(legs)              = 100
spread_width(legs, "JADE_LIZARD") = 22800
```

### Fix direction
Grade with call-wing width or true risk capital (`max_loss + credit` in **points of risk**, not raw strike as a faux spread width). Keep veto on call wing if that remains the JL definition check.

---

## Bug 13 — `mid_price` ignores `last_price`

**Status: FIXED (2026-09-15)** — `mid_price`: settle → close → last.

### Severity
Hard veto or stale prices. Live ATM IV extracts `last_price|close|settle`; legs only use `settle` then `close`.

### Trace
`leg_builder.mid_price` vs `suggestion_engine._compute_live_atm_iv_rank` price extract. Row with only `last_price>0` → ATM IV OK, `suggested_price=0` → “Chain too thin”.

### Proof

```text
mid_price({last_price:150, settle_price:0, close_price:0}) → 0.0
```

Current Zerodha/NSE providers usually mirror LTP into settle — so often masked — but the path still disagrees with itself.

### Fix direction
Align `mid_price` with live extract order: `last_price or settle or close`.

---

## Bug 14 — Calendar allows mismatched near/far strikes (silent diagonal)

**Status: FIXED (2026-09-15)** — require shared strike; raise on silent diagonal.

### Severity
Builds a diagonal, labels/economics treat it as same-strike calendar.

### Trace
`build_calendar_spread`: if near ATM ∉ far strikes, far keeps its own ATM. No raise. BE / max-profit still assume a single ATM calendar.

### Proof

```text
near {23000,23050}, far {23025,23075}, spot 23000
→ SELL 23000 near / BUY 23025 far — no error
```

### Fix direction
Require identical strike (or veto / StrategyVeto when far cannot match near ATM).

---

## Path map (for navigation)

| Step | File |
|------|------|
| Orchestrator | `lifecycle/suggestion_engine.py` |
| Confidence gate | `engine/confidence.py` |
| Strategy pick + assemble | `engine/strategy_selector.py` |
| Legs / width / PoP | `engine/leg_builder.py` |
| Indicator VIX None | `engine/indicators.py` |
| Soft-pass config | `config.py` → `strategy_min_soft_pass` |
| Writing sell-min map | `config.py` → `strategy_iv_premium_sell_min` |
