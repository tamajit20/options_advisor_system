# Bug findings: suggestion_engine → confidence → strategy_selector → leg_builder

Date: 2026-09-13  
Scope: failure modes only (not design opinions).

---

## Bug 1 — Missing VIX: confidence passes, assemble crashes, underlying silently dropped

### Severity
Hard failure. Entire underlying evaluation dies with `TypeError`. No suggestion and no `NoSuggestion` persisted.

### Trace

1. **`engine/confidence.py`** — `vix_close is None` → `PASS_WARN` (not a fail). Soft tally can still yield `all_passed=True`.

```python
def _vix_gate():
    if indicators.vix_close is None:
        return _PASS_WARN, "VIX data not available for today — cannot evaluate VIX regime"
```

2. **`engine/strategy_selector.py` → `_explain`** — after legs are built, plain-English formatting assumes a float:

```python
parts.append(
    f"IV Rank {iv_rank:.0f}, trend {indicators.trend.lower()}, "
    f"VIX {indicators.vix_close:.1f} ({indicators.vix_regime.lower()})."
)
```

`None:.1f` → `TypeError: unsupported format string passed to NoneType.__format__`.

3. **`lifecycle/suggestion_engine.py` → `_evaluate_underlying`** — only catches `StrategyVeto`. The `TypeError` escapes to the outer loop:

```python
try:
    sugs, nss = _evaluate_underlying(...)
except Exception:
    logger.exception("Suggestion eval failed for %s", symbol)
    continue
```

Result: no suggestion, no no-suggestion row — the symbol vanishes for that run.

### Why this path is reachable
`engine/indicators.py` sets `vix_close=None` when `vix_history` is empty. That is a normal data gap. Confidence treats it as pass-with-warn; selector assumes a float.

### Proof (repro)

```text
confidence.all_passed= True   # VIX gate PASS_WARN
assemble CRASH: TypeError unsupported format string passed to NoneType.__format__
```

### Fix direction
- Guard `_explain` for `vix_close is None` / missing regime, **or**
- Treat missing VIX as a hard/soft fail consistently with what `_explain` requires, **and**
- Catch non-`StrategyVeto` assemble failures per underlying and persist a `NoSuggestion` instead of swallowing the symbol.

---

## Bug 2 — Collapsed iron-condor wings: zero width, zero max loss, veto skipped

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

### Fix direction
- Reject structures where any hedge pair shares a strike (raise `ValueError` / `StrategyVeto`).
- Treat `spread_width == 0` for credit strategies as a hard veto, not a skip.

---

## Bug 3 — `strategy_min_soft_pass` counts only status `"PASS"`, not confidence “passed”

### Severity
Logic mismatch on the confidence → selector handoff. Strategies that confidence already cleared can be hard-vetoed for the wrong reason.

### Trace

1. **`contracts.ConfidenceCheck.passed`** — `PASS`, `PASS_WARN`, and `PASS_ERROR` all count as passed.

2. **`engine/confidence.py`** — intentional `PASS_WARN` bands exist (e.g. IV/HV between buy_pass and buy_max for buying regime).

3. **`engine/strategy_selector.py` → `assemble_suggestion`**:

```python
soft_pass_count = sum(1 for c in soft_checks if c.status == "PASS")
```

`PASS_WARN` does **not** count. So `LONG_CALL` / `LONG_PUT` (config requires 8/8) can never clear when any soft gate is an intentional warn — even when `confidence.all_passed` is True and per-strategy IV/HV caps would allow the trade.

### Proof (repro)

```text
LONG_CALL path: all_passed True
soft statuses include IV premium PASS_WARN
exact PASS count 6
passed prop count 7
LONG_CALL StrategyVeto: LONG_CALL requires 8/8 soft gates, got 6/8
```

### Fix direction
- Count with `c.passed` (or explicitly include `PASS_WARN` / `PASS_ERROR` if that is the intended bar), **or**
- Document and enforce that `strategy_min_soft_pass` means “exact PASS only” and stop emitting `PASS_WARN` for bands that are meant to remain tradeable for those strategies.

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
