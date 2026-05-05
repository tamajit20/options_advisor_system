# Options Advisor System — Developer Handover (Granular)

## 1. Project Structure & Conventions
- **Language:** Python 3.11
- **Web UI:** Flask (dashboard/server.py, port 5001)
- **Database:** SQL Server Express (host.docker.internal,1433, DB: OptionsAdvisorDB)
- **Deployment:** Docker Compose (container: stock_options_advisor)
- **Timezone:** Asia/Kolkata (all scheduling, trading windows)
- **Config:** All thresholds, URLs, and per-strategy overrides are in config.py (never hardcoded elsewhere)
- **Data Shapes:** Only contracts.py dataclasses are allowed for cross-module data
- **Module Boundaries:**
  - downloader/ → database/ → engine/ → lifecycle/ → dashboard/
  - engine/ is pure logic (no DB, HTTP, or I/O)
  - database/ wraps pyodbc; never imported by engine/
  - lifecycle/ orchestrates downloader → engine → database
- **Testing:** pytest, no live I/O, coverage targets: engine ≥ 90%, overall ≥ 75%
- **All conventions:** See .github/copilot-instructions.md

## 2. Job Triggering & Date Logic
- **Manual job triggers** (via dashboard) support an optional date override for jobs in DATE_OVERRIDE_JOBS (dashboard/static/dashboard.js)
- **If you enter a date in the modal:** That date is used for the job (e.g., fo_bhav_download, spot_bhav_download, etc.)
- **If you leave the date blank:** The job runs for today (server's IST date)
- **Backend:** Validates the date and passes it to the orchestrator, which defaults to today_ist() if not provided
- **To restrict the modal:** Edit the set in dashboard/static/dashboard.js

## 3. Engine Improvements (2026)
- **PoP for long-premium strategies:** Uses BE-crossing probability (lognormal)
- **IV/HV gating:** Now stricter and DTE-aware
- **Profit targets:** DTE/IV-aware and per-strategy (configurable)
- **Config isolation:** All config is per-strategy (no cross-leakage)
- **All config:** Single source of truth is config.py

## 4. UI/UX & Dashboard
- **Modal date input:** Appears only for jobs in DATE_OVERRIDE_JOBS
- **All job triggers:** POST to /api/jobs/<job_name>/trigger
- **Modal dialog:** Was previously shown for all jobs; now restricted to only those in the override set
- **Discussions:**
  - User wanted modal only for specific jobs, not all
  - Confirmed modal logic is controlled by DATE_OVERRIDE_JOBS set
  - Confirmed backend honors date override and defaults to today if blank

## 5. Operational Notes
- **BANKNIFTY/FINNIFTY weekly options:** Discontinued ~Nov 2024; system handles as monthly when DTE ≤ 21
- **BACKLOG.md:** Deprecated; use FUTURE_ENHANCEMENT_SCOPES.md and @pytest.mark.future stubs
- **No live I/O in tests:** Use fixtures/mocks

## 6. Testing & Coverage
- **Tests:** Located in tests/ (pytest)
- **Coverage targets:** engine ≥ 90%, overall ≥ 75%
- **No live I/O in tests:** Use fixtures/mocks
- **Test conventions:** See .github/copilot-instructions.md

## 7. Deferred/Future Work
- **All deferred work:**
  - Add to FUTURE_ENHANCEMENT_SCOPES.md
  - Add a skipped test stub in tests/ with @pytest.mark.future
- **When implementing:** Remove skip marker, implement, and update FUTURE_ENHANCEMENT_SCOPES.md

## 8. Key Discussions (from chat)
- **Modal dialog:**
  - User: "Any job I run it says the attached pop up. But I wanted it only for a particular job."
  - Analysis: Modal is shown for all jobs in DATE_OVERRIDE_JOBS. Restrict by editing the set.
  - User: "If I pass a date, will it work?" — Yes, backend and orchestrator honor the date.
  - User: "Which date if I don't override?" — Defaults to today (Asia/Kolkata).
  - User: "Make a handover file for the next model." — This file.
- **Engine improvements:**
  - All four expert-flagged improvements implemented and tested
  - Per-strategy config isolation enforced
  - DTE/IV-aware profit targets validated
- **Testing:**
  - All new logic covered by tests
  - No live DB or NSE calls in tests
- **Operational validation:**
  - 946 tests passing, deployment logs clean
  - Modal dialog restriction validated

## 9. Handover Checklist
- [x] All conventions and boundaries documented
- [x] Engine and UI/UX improvements summarized
- [x] Job/date override logic explained
- [x] Deferred work process described
- [x] Key discussions and decisions included
- [x] See .github/copilot-instructions.md for full rules

---
**If you are a new developer or model, read .github/copilot-instructions.md and this file first.**
