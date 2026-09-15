# Bug findings: suggestion_engine → confidence → strategy_selector → leg_builder

Date: 2026-09-13  
Last review: 2026-09-15  
Scope: failure modes only (not design opinions).

## Fix status summary

| # | Issue | Status | Why |
|---|---|---|---|
| **1** | Missing VIX → `TypeError` in `_explain` → underlying dropped | **FIXED** | Pure tech crash (`None:.1f`). Guard in `engine/strategy_selector.py` `_explain`; test `test_explain_tolerates_missing_vix_close`. No confidence/config change. Outer-loop “persist NoSuggestion on any exception” **not** done (that would change sit-out bookkeeping, not stop the crash). |
| **2** | Collapsed iron-condor wings (width 0 / max loss 0) | **NOT FIXED** | Not a type/exception crash. Fixing means new structure/veto rules (reject same-strike wings, treat width 0 as hard veto) — product/risk policy, not a format bug. Deferred until you ask for that change. |
| **3** | `strategy_min_soft_pass` counts `"PASS"` only, not `.passed` | **NOT FIXED** | Intentional gate semantics. Existing test `test_long_call_pass_warn_does_not_count_as_soft_pass` encodes exact-`PASS` counting. Changing it would alter who gets cards (business/config). Deferred. |

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

```python
def _vix_gate():
    if indicators.vix_close is None:
        return _PASS_WARN, "VIX data not available for today — cannot evaluate VIX regime"
```

2. **`engine/strategy_selector.py` → `_explain`** — *(was)* after legs are built, plain-English formatting assumed a float:

```python
# BEFORE (crashed): VIX {indicators.vix_close:.1f}
# AFTER:  if vix_close is None → "VIX n/a"
```

3. **`lifecycle/suggestion_engine.py`** — only catches `StrategyVeto` inside assemble; any leftover `TypeError` still escapes to the outer loop (rare now that `_explain` is guarded).

### Why this path was reachable
`engine/indicators.py` sets `vix_close=None` when `vix_history` is empty. Confidence treats it as pass-with-warn; selector assumed a float.

---

## Bug 2 — Collapsed iron-condor wings: zero width, zero max loss, veto skipped

**Status: NOT FIXED** — deferred (policy / structure, not a crash).

### Why not fixed
Rejecting same-strike wings or forcing a width-0 veto changes which condors can be suggested. That is risk/business logic, not a type mismatch. Left as documented risk until you request a product decision.

### Severity
Hard logic failure. Invalid structure can be assembled and sized as if risk were defined.

### Trace

1. **`engine/leg_builder.py` → `build_iron_condor`** — long wings use `closest_strike` on `short ± wing_width`. Near the edge of a thin chain, long and short map to the **same** strike:

```text
strikes [('PE','SELL',24800), ('PE','BUY',24800), ('CE','SELL',25200), ('CE','BUY',25200)]
width 0.0  net 0.0  max_profit 0.0  max_loss 0.0
```

2. **`engine/strategy_selector.py` → credit-to-width veto** — only fires when `spread_w_for_veto > 0`. Width 0 → veto skipped.

3. **`lifecycle/suggestion_engine.py` → `_assemble_sized_suggestion`** — lot sizing only runs when `_one_lot_max_loss > 0`. Max loss 0 → stays at 1 lot and can proceed.

### Proof (repro)

Thin chain around spot with `expected_move` past the wing edge → `build_iron_condor` returns same-strike buy/sell pairs; `spread_width` / `max_profit_loss` report 0.

### Possible fix (if approved later)
- Reject structures where any hedge pair shares a strike (raise `ValueError` / `StrategyVeto`).
- Treat `spread_width == 0` for credit strategies as a hard veto, not a skip.

---

## Bug 3 — `strategy_min_soft_pass` counts only status `"PASS"`, not confidence “passed”

**Status: NOT FIXED** — deferred (intentional semantics / card volume).

### Why not fixed
Counting `c.passed` (include `PASS_WARN`) would let more `LONG_CALL` / `LONG_PUT` (and similar) through. Current code + test require exact `"PASS"`. That is a product gate choice, not a crash.

### Severity
Logic mismatch on the confidence → selector handoff (or intentional strictness, depending on product view).

### Trace

1. **`contracts.ConfidenceCheck.passed`** — `PASS`, `PASS_WARN`, and `PASS_ERROR` all count as passed.

2. **`engine/confidence.py`** — intentional `PASS_WARN` bands exist (e.g. IV/HV between buy_pass and buy_max for buying regime).

3. **`engine/strategy_selector.py` → `assemble_suggestion`**:

```python
soft_pass_count = sum(1 for c in soft_checks if c.status == "PASS")
```

`PASS_WARN` does **not** count. So `LONG_CALL` / `LONG_PUT` (config requires 8/8) can never clear when any soft gate is an intentional warn — even when `confidence.all_passed` is True.

### Proof (repro)

```text
LONG_CALL path: all_passed True
soft statuses include IV premium PASS_WARN
exact PASS count 6
passed prop count 7
LONG_CALL StrategyVeto: LONG_CALL requires 8/8 soft gates, got 6/8
```

### Possible fix (if approved later)
- Count with `c.passed`, **or**
- Document that `strategy_min_soft_pass` means “exact PASS only” and keep current tests.

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
