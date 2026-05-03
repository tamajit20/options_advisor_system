# Copilot Instructions — Options Advisor System

This file is auto-loaded by GitHub Copilot at the start of every session in this workspace. It encodes durable conventions that should survive across sessions and contributors.

---

## Project at a glance

- **Language**: Python 3.11
- **Web framework**: Flask (dashboard at `dashboard/server.py`, port 5001)
- **Database**: SQL Server Express on `host.docker.internal,1433` (DB: `OptionsAdvisorDB`)
- **Deployment**: Docker (container `stock_options_advisor`, see `docker-compose.yml`)
- **Timezone**: All scheduling and trading windows are `Asia/Kolkata`
- **Single source of truth for config**: `config.py` (no hardcoded thresholds, URLs, or rates anywhere else)
- **Single source of truth for inter-module data shapes**: `contracts.py` (dataclasses only)

## Module boundaries (strict)

```
downloader/   →  database/   →  engine/   →  lifecycle/   →  dashboard/
                                  ↑
                            scheduler/, simulation/
```

- `engine/` is **pure logic** — no DB, no HTTP, no I/O. Always testable without mocks.
- `database/` wraps pyodbc; never imported by `engine/`.
- `lifecycle/` orchestrates downloader → engine → database; no business logic of its own.

---

## Future-scope convention — CRITICAL

Future enhancements (deferred work) are tracked in **two synchronized places**:

1. **`FUTURE_ENHANCEMENT_SCOPES.md`** at the repo root (prose backlog)
2. **`@pytest.mark.future` skipped test stubs** in `tests/` (executable backlog)

### Rule — every future enhancement updates BOTH

When the user mentions a deferred enhancement (cues: "later", "eventually", "TODO", "future scope", "park this", "would be nice", "not now"), do BOTH in the same change:

1. **Append the item** to `FUTURE_ENHANCEMENT_SCOPES.md` under the appropriate priority section (🔴 Engine Correctness / 🔴 Risk & Monitoring / 🟡 Strategy & Regime Coverage / 🟡 Position Sizing / 🟡 Data Quality / 🟢 Simulation / 🟢 Code Quality / 📋 Discussed but Deferred).
2. **Add a skipped test stub** in the matching test file:
   ```python
   @pytest.mark.future
   @pytest.mark.skip(reason="future: <one-line summary> (FUTURE_ENHANCEMENT_SCOPES.md → <section>)")
   def test_<feature_name>():
       """Describe the expected behaviour after the fix."""
       pass
   ```

If the user's intent is ambiguous, ask: **"Future-scope stub, or implement now?"**

### Rule — implementing a future-scope item

When the user asks to implement a future-scope item:
1. Remove the `@pytest.mark.skip` decorator AND the `@pytest.mark.future` marker
2. Flesh out the test body to assert real expected behaviour
3. Implement the feature
4. **Delete the corresponding entry** in `FUTURE_ENHANCEMENT_SCOPES.md` (or move it to a `## Resolved` section with the commit hash)

All four steps belong in the same commit. Do not leave the doc out of sync with the tests.

---

## Testing conventions

- **Framework**: pytest (config in `pytest.ini`)
- **Layout**: `tests/test_<package>/test_<module>.py` mirrors source layout
- **Markers** (declared in `pytest.ini`): `unit`, `integration`, `slow`, `future`
- **Fixtures**: shared via `tests/conftest.py` — prefer reusing `sample_chain`, `sample_indicators`, `make_leg`, `mock_db` over creating new ones
- **Mocking**: pytest-mock for unit-level mocks, `responses` for HTTP, `freezegun` for time
- **No live I/O**: tests must never hit real DB, real NSE endpoints, or read system clock
- **Coverage targets**: engine ≥ 90%, overall ≥ 75% (no enforced gate yet)

When adding new feature code, write a unit test in the same change. When fixing a bug, write a failing test that reproduces it before the fix.

---

## Code conventions

- Dataclasses (`contracts.py`) are the only allowed cross-module data shapes
- Configuration via `STRATEGY_CONFIG[...]` etc. in `config.py`; never inline a magic number
- Custom exceptions in `exceptions.py`: `RecoverableError` / `JobFailure` / `CriticalError` / `StrategyVeto`
- Type hints on public functions; `from __future__ import annotations` at the top
- No `print()` — use the logging utilities in `utils.py`
- Docker rebuild required for Python changes (`docker compose build --no-cache && docker compose up -d`); static assets in `dashboard/static/` can be hot-deployed via `docker cp`

---

## Operational notes

- Live NSE endpoints (option chain JSON) are accessible without auth — useful for intraday monitoring
- BANKNIFTY/FINNIFTY weekly options were discontinued ~Nov 2024; system handles them via monthly expiry when DTE ≤ 21
- `BACKLOG.md` was deprecated and replaced by `FUTURE_ENHANCEMENT_SCOPES.md` (commit `b0b1117`)
