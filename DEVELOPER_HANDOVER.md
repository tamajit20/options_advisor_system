# Options Advisor System — Developer Handover Notes

## Project Overview
- **Language:** Python 3.11
- **Web UI:** Flask (dashboard/server.py, port 5001)
- **Database:** SQL Server Express (host.docker.internal,1433, DB: OptionsAdvisorDB)
- **Deployment:** Docker Compose (container: stock_options_advisor)
- **Timezone:** Asia/Kolkata (all scheduling, trading windows)
- **Config:** All thresholds, URLs, and per-strategy overrides are in config.py (never hardcoded elsewhere)
- **Data Shapes:** Only contracts.py dataclasses are allowed for cross-module data

## Module Boundaries
- downloader/ → database/ → engine/ → lifecycle/ → dashboard/
- engine/ is pure logic (no DB, HTTP, or I/O)
- database/ wraps pyodbc; never imported by engine/
- lifecycle/ orchestrates downloader → engine → database

## Job Triggering & Date Logic
- Manual job triggers (via dashboard) support an optional date override for jobs in DATE_OVERRIDE_JOBS (dashboard/static/dashboard.js)
- If you enter a date in the modal, that date is used for the job (e.g., fo_bhav_download, spot_bhav_download, etc.)
- If you leave the date blank, the job runs for today (server's IST date)
- The backend validates the date and passes it to the orchestrator, which defaults to today_ist() if not provided

## Testing & Coverage
- Tests in tests/ (pytest)
- Coverage targets: engine ≥ 90%, overall ≥ 75%
- No live I/O in tests (use fixtures/mocks)

## Engine Improvements (2026)
- PoP for long-premium strategies uses BE-crossing probability
- IV/HV gating is stricter and DTE-aware
- Profit targets are DTE/IV-aware and per-strategy
- All config is isolated per-strategy (no cross-leakage)

## UI/UX
- Modal date input appears only for jobs in DATE_OVERRIDE_JOBS
- To restrict, edit the set in dashboard/static/dashboard.js
- All job triggers POST to /api/jobs/<job_name>/trigger

## Operational Notes
- BANKNIFTY/FINNIFTY weekly options discontinued ~Nov 2024; system handles as monthly when DTE ≤ 21
- BACKLOG.md is deprecated; use FUTURE_ENHANCEMENT_SCOPES.md and @pytest.mark.future stubs

## Handover
- All critical conventions are in .github/copilot-instructions.md
- For any deferred work, update both FUTURE_ENHANCEMENT_SCOPES.md and add a skipped test stub
- See README.md for quickstart and deployment

---
**If you are a new developer, read .github/copilot-instructions.md and this file first.**
