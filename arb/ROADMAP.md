# NSE–BSE Arbitrage Roadmap

Decided execution order. **No live execution until paper phase validates logic.**

---

## Phase 1 — Built (monitoring only)

Implemented in commit `8c9c5e7`:

- **`ArbGapEngine`** — in-memory gap episodes; `tick.arb` subscription, NSE/BSE leg pairing
- **Pair mapping** — `ArbPairRepo`
- **`arb_gaps` DB** — background writer (insert/update/close)
- **Dashboard** — Arb Live, Gap History, Pairs (`/api/arb/*`, SSE live gaps)

**Deploy:** VM deploy may be pending (SSH timeout). Verify live ticks and dashboard on target host.

---

## Phase 2 — Data review (FIRST — no execution)

Review existing Phase 1 data before any simulated or real trades:

- Gap History + Arb Live: frequency, size, duration, direction
- `arb_gaps` analysis: session patterns, symbol coverage, data quality
- Pair mapping completeness and tick freshness

**Out of scope:** orders, paper fills, broker integration.

---

## Phase 3 — Paper trading (THEN)

Live-quote-backed fill simulator after data review sign-off:

- Fills at **bid/ask** (not mid/LTP-only)
- **UNHEDGED** protection (like Scout `UNPROTECTED`): detect one-legged fills, auto-unwind, reconciliation
- Paper only — no broker orders

**Limits:** cannot test real broker fills, exchange latency, or partial-fill behavior; logic + simulated failures only.

---

## Phase 4 — Live (LATER — not now)

1. Micro live — small size, strict caps
2. Scaled live — after micro-live metrics acceptable

---

## Flow

```
Phase 1 (done) → Data review → Paper + UNHEDGED → Micro live → Scaled live
```
