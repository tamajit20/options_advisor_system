# Tests — Options Advisor System

Pytest suite covering the engine, lifecycle, downloader, database, simulation, and dashboard modules.

---

## How to run

**Quick run** (verbose, all tests):
```powershell
pytest -v
```

**With coverage** (HTML report at `tests/coverage_html/index.html`):
```powershell
pytest --cov=engine --cov=lifecycle --cov=database --cov=downloader --cov=simulation --cov=dashboard --cov-report=term-missing --cov-report=html:tests/coverage_html
```

Or use the convenience script:
```powershell
.\run_tests.ps1
```

**Filter by marker**:
```powershell
pytest -m unit            # pure-logic tests (no I/O)
pytest -m integration     # mocked I/O tests
pytest -m "not slow"      # skip slow tests
pytest -m future          # show deferred placeholders (all skipped)
```

---

## Layout

```
tests/
├── conftest.py              # shared fixtures: sample_chain, mock_db, indicators
├── test_engine/             # pure-logic unit tests (no mocks needed)
├── test_database/           # repo CRUD with mocked pyodbc       (Phase 3)
├── test_downloader/         # CSV/HTTP parsing with mocked NSE   (Phase 3)
├── test_lifecycle/          # orchestrators with mocked DB       (Phase 4)
├── test_simulation/         # backtest math                      (Phase 4)
└── test_dashboard/          # Flask test client                  (Phase 4)
```

Phase 2 (engine) is implemented. Phases 3 + 4 are intentionally left as future scope — placeholder folders/test files will be added as those layers gain coverage.

---

## Future-scope handling — IMPORTANT CONVENTION

Future enhancements live in two synchronized places:

1. **`FUTURE_ENHANCEMENT_SCOPES.md`** (prose backlog at repo root)
2. **`@pytest.mark.future` skipped test stubs** (executable backlog inside this folder)

**Whenever a deferred enhancement is identified — whether discussed in conversation or written into the doc — both must be updated together.** Pattern:

```python
@pytest.mark.future
@pytest.mark.skip(reason="future: <one-line summary> (FUTURE_ENHANCEMENT_SCOPES.md → <section>)")
def test_<feature_name>():
    """Describe the expected behaviour after the fix."""
    pass
```

When the enhancement is **implemented**:
1. Remove `@pytest.mark.skip` decorator (keep `@pytest.mark.future` removed too)
2. Flesh out the test body
3. Delete or move-to-Resolved the corresponding entry in `FUTURE_ENHANCEMENT_SCOPES.md`

This keeps the deferred backlog visible in the test report (`pytest -m future`) instead of buried in a doc.

---

## Adding new tests

1. **New module gets a new test file**: `tests/test_<package>/test_<module>.py`
2. **Mirror the source layout** under `tests/`
3. **Use existing fixtures** from `conftest.py` (`sample_chain`, `sample_indicators`, `make_leg`, `mock_db`) before creating new ones
4. **Tag with markers**: `@pytest.mark.unit` (default — can omit), `@pytest.mark.integration`, `@pytest.mark.slow`, `@pytest.mark.future`
5. **One concept per test** — name `test_<thing>_<expected_behaviour>`
6. **Parametrize** for repeated logic with input variations

---

## Mock conventions

- **Database**: use the `mock_db` fixture (a `MagicMock` shaped like `SQLServerConnection`). Configure `fetch_one`/`fetch_all` return values per-test.
- **HTTP**: use the `responses` library (added in Phase 3) — never hit live NSE endpoints
- **Time**: use `freezegun` (`@freeze_time("2026-04-30")`) — never read system clock
- **Scheduler / Flask**: import + use directly (Flask test client; no APScheduler internals)

---

## Coverage expectations

| Phase | Scope | Target |
|-------|-------|--------|
| Phase 2 | `engine/` (pure logic) | ≥ 90% line coverage |
| Phase 4 | Whole project | ≥ 75% line coverage |

There is no `--cov-fail-under` gate yet — add one once the baseline is stable.
