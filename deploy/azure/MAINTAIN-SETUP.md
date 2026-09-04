# Maintain setup scripts (required when changing infra)

When you add tables, jobs, or deploy scripts, update this checklist **in the same PR/commit**.

Run before merge:

```bash
python scripts/validate_setup_sync.py
pytest tests/test_database/test_schema.py tests/test_scheduler/test_scheduler.py -q
```

---

## New historical DB table

1. `database/schema.py` — CREATE TABLE + add to `list_tables()`
2. `database/archive_registry.py` — add `ArchiveTableSpec` (unless log-only or never archive)
3. `readmefirst.txt` Section 7 — only if special retention rules
4. `config.py` — `RETENTION_CONFIG` key if new hot window
5. `scheduler/scheduler.py` — if log table: `job_weekly_log_cleanup`; else archive handles via registry

**Skip archive** for: config, flags, calendars, hot-only tables.

---

## New log / audit table (delete only)

1. `database/schema.py` + `list_tables()`
2. `config.py` — `RETENTION_CONFIG` days
3. `scheduler/scheduler.py` — `job_weekly_log_cleanup` delete call
4. `readmefirst.txt` Section 5 log retention line

---

## New scheduler job

1. `config.py` — `SCHEDULER_CONFIG["jobs"]` + timeout
2. `scheduler/scheduler.py` — handler + `JOB_FUNCS`
3. `dashboard/server.py` — `_JOB_META` entry
4. `readmefirst.txt` Section 5 schedule table
5. `deploy/azure/OPERATIONS.md` — if ops-relevant

Align job time with **VM uptime Mon–Fri 08:55–15:45 IST** (no weekend-only jobs unless VM is on).

---

## New deploy / laptop script

1. `deploy/README.md` — script row
2. `readmefirst.txt` Section 4 — script row
3. `deploy/azure/setup-new-environment.ps1` — wire in if part of greenfield bootstrap
4. `deploy/azure/setup-manifest.json` + `Test-EnvironmentSetup.ps1` — if verifiable automatically

---

## New archive / backup behaviour

1. `lifecycle/archive_export.py`, `deploy/archive-export.sh`
2. `deploy/azure/pull-archive-and-merge.ps1`, `scripts/merge_archive_into_local.py`
3. `deploy/azure/ARCHIVE-AUTOMATION.md`, `readmefirst.txt` Section 5

---

## After any bootstrap change

Update **Last updated** date in `readmefirst.txt` Section header.
