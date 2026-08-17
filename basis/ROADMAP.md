# Cash-Futures Basis Monitor Roadmap

Decided execution order. **No live execution until paper phase validates logic.**

---

## Phase 1 — Built (monitoring only)

- **`BasisEngine`** — in-memory basis episodes; `tick.basis` subscription, NSE spot + NFO near-month fut pairing
- **Pair mapping** — `BasisPairRepo` (NSE EQ + earliest NFO FUT expiry ≥ today)
- **`basis_episodes` DB** — background writer (insert/update/close)
- **Dashboard** — Basis Live, History, Pairs, Config (`/api/basis/*`, SSE live basis)

**Out of scope for Phase 1:** calendar spreads, box spreads, crypto basis, order execution.

---

## Phase 2 — Data review (FIRST — no execution)

Review Phase 1 data before any simulated or real trades:

- Basis History + Live: frequency, magnitude, duration, contango/backwardation patterns
- `basis_episodes` analysis: session patterns, symbol coverage, roll-week behaviour
- Pair mapping completeness and tick freshness

---

## Phase 3 — Paper trading (THEN)

Live-quote-backed fill simulator after data review sign-off:

- Fills at **bid/ask** (not mid/LTP-only)
- Roll / expiry handling for near-month fut leg
- Paper only — no broker orders

---

## Phase 4 — Live (LATER — not now)

1. Micro live — small size, strict caps
2. Scaled live — after micro-live metrics acceptable

---

## Flow

```
Phase 1 (done) → Data review → Paper → Micro live → Scaled live
```
