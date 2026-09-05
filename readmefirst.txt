================================================================================
OPTIONS ADVISOR — READ THIS FIRST
================================================================================

For humans:  Open this repo in Cursor Agent mode and say:

    Follow readmefirst.txt and bootstrap everything on this machine.

For AI agents: This file is the single entry point. Execute the playbook below.
Do not ask the user to paste long prompts. Read linked docs only when you need
detail. Run commands yourself. Ask the user ONLY for secrets and Azure Portal
values you cannot infer.

Last updated: 2026-09-05 (keep in sync — see Section 8)


================================================================================
SECTION 1 — WHAT YOU ARE SETTING UP
================================================================================

  Component              Where              Purpose
  ---------------------  -----------------  ------------------------------------
  Flask dashboard        Azure VM :5001     Trading UI, config, jobs
  SQL Server             Docker on VM       Hot operational DB (OptionsAdvisorDB)
  SQL Server Express     Windows laptop     Cumulative archive (OptionsAdvisorDB_Archive)
  Scheduler              VM container       EOD, live suggestions, archive, logs
  Azure Automation       Azure cloud        VM start 08:55 / stop 15:45 Mon-Fri IST
  Windows Task Scheduler Laptop             Mon-Fri 09:15 pull archive + merge

  Manual forever: Zerodha login each trading morning (dashboard key icon).


================================================================================
SECTION 2 — HUMAN PREREQUISITES (before AI runs scripts)
================================================================================

  [ ] Git, Python 3, Azure CLI, OpenSSH client installed on laptop
  [ ] SQL Server Express + SSMS/sqlcmd on laptop (for archive merge)
  [ ] Azure VM: Ubuntu 22.04, SSH .pem key saved, public IP noted
  [ ] Repo cloned to laptop
  [ ] Secrets ready: DB password, Zerodha API key/secret, OPT_DASHBOARD_API_KEY
  [ ] Optional: existing OptionsAdvisorDB-*.bak for restore


================================================================================
SECTION 3 — AI BOOTSTRAP PLAYBOOK (execute in order)
================================================================================

  Step  Action
  ----  ---------------------------------------------------------------------
  1     Read deploy/azure/SETUP-CHECKLIST.md and deploy/azure/ARCHIVE-AUTOMATION.md

  2     If deploy/azure/laptop.config.ps1 missing:
          copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
        Ask user for: VmHost, SshKeyPath, AzureResourceGroup, AzureVmName,
        LocalSqlServer (e.g. MACHINENAME\SQLEXPRESS). Fill laptop.config.ps1.

  3     If .env.docker missing:
          copy .env.docker.example .env.docker
        Ask user for: MSSQL_SA_PASSWORD, OPT_DB_PASSWORD (same value),
        OPT_ZERODHA_API_KEY, OPT_ZERODHA_API_SECRET, OPT_DASHBOARD_API_KEY.
        Never commit .env.docker or laptop.config.ps1.

  4     az login   (if Azure CLI present and VM/uptime setup needed)

  5     Greenfield (new VM + laptop):
          .\deploy\azure\setup-new-environment.ps1
        With existing DB backup:
          .\deploy\azure\setup-new-environment.ps1 -RestoreFromBackup "PATH\to\file.bak"
        Laptop only (VM already running):
          .\deploy\azure\setup-laptop.ps1

  6     Validate:
          python scripts/validate_setup_sync.py
          .\deploy\azure\Test-EnvironmentSetup.ps1
        Fix every critical [XX] failure. Re-run until exit code 0.

  7     On VM (via SSH): ensure latest code and archive scripts deployed:
          cd ~/options_advisor_system && git pull origin master
          chmod +x deploy/*.sh deploy/archive-export.sh deploy/archive-truncate-vm.sh
          ./deploy/vm-restart.sh

  8     Report to user:
          - Dashboard URL: http://<VmHost>:5001
          - Local archive DB: OptionsAdvisorDB_Archive
          - What's automated (Section 5)
          - What's manual (Zerodha login)


================================================================================
SECTION 4 — KEY SCRIPTS (do not duplicate logic elsewhere)
================================================================================

  Script                                    Purpose
  ----------------------------------------  ---------------------------------
  deploy/azure/setup-new-environment.ps1    One-shot: laptop + VM + uptime + verify
  deploy/azure/setup-laptop.ps1             Laptop folders + archive task only
  deploy/azure/Test-EnvironmentSetup.ps1    Checklist: tools, SSH, dashboard, tasks
  deploy/azure/remote-vm-install.ps1        VM Docker + app + port 5001
  deploy/azure/VMUpTimeConfiguration.ps1    Azure start/stop schedules
  deploy/azure/restore-database-from-laptop.ps1   Laptop .bak -> VM
  deploy/azure/backup-database-to-laptop.ps1      VM .bak -> laptop
  deploy/azure/pull-archive-and-merge.ps1   Download archive chunk + merge locally
  deploy/azure/register-laptop-archive-task.ps1   Windows scheduled task
  deploy/archive-export.sh                  VM: export *_Archive to .bak (Fri)
  deploy/archive-truncate-vm.sh               VM: clear *_Archive after laptop ACK
  deploy/vm-restart.sh                      Rebuild/restart app container on VM
  deploy/backup.sh                          Hot DB backup on VM
  scripts/merge_archive_into_local.py       Merge weekly .bak into archive DB
  scripts/validate_setup_sync.py            Repo setup files in sync with schema


================================================================================
SECTION 5 — AUTOMATED SCHEDULE (no daily user action)
================================================================================

  VM Mon-Fri 08:55-15:45     Azure Automation (VMUpTimeConfiguration.ps1)

  VM Friday 09:30            weekly_archive      hot rows -> *_Archive
  VM Friday 09:35            weekly_log_cleanup  delete logs only
  VM Friday 15:36            archive_export      .bak + PENDING.json on VM
  VM Friday 15:38            db_backup           hot DB snapshot

  Laptop Mon-Fri 09:15       Task OptionsAdvisor-ArchiveMerge
                               pull-archive-and-merge.ps1

  Log retention (VM delete):  system_logs 7d | job_log 7d | zerodha_execution_jobs 30d
  Hot retention (VM archive):  hot_archive_keep_days = 365 (all tables incl. broker orders)


================================================================================
SECTION 6 — CONFIG FILES (gitignored — never commit secrets)
================================================================================

  deploy/azure/laptop.config.ps1    VM IP, SSH key, Azure RG, local SQL paths
  .env.docker                       DB passwords, Zerodha, dashboard API key (on VM too)

  Templates: laptop.config.ps1.example, .env.docker.example


================================================================================
SECTION 7 — DATABASE & ARCHIVE TABLES
================================================================================

  Schema source:     database/schema.py  (list_tables())
  Archive registry:  database/archive_registry.py  (ARCHIVE_TABLE_SPECS)
  Archive logic:     database/archive_repo.py, lifecycle/archive_orchestrator.py
  Log tables (delete only, no _Archive): options_system_logs, options_job_log,
                                         options_zerodha_execution_jobs

  Never archive: options_config, options_runtime_flags, options_lot_sizes,
                 options_expiry_calendar, options_events_calendar,
                 options_trade_mtm_snapshot (active trades only)

  Local cumulative history: OptionsAdvisorDB_Archive (laptop SQL Express)


================================================================================
SECTION 8 — MAINTENANCE (developers & AI: keep setup in sync)
================================================================================

  When you change the system, update ALL rows that apply in:
  deploy/azure/MAINTAIN-SETUP.md

  Then run:
    python scripts/validate_setup_sync.py
    pytest tests/test_database/test_schema.py -q

  Cursor rule .cursor/rules/options-advisor-setup.mdc enforces this for all agents.

  Quick matrix:

  Change                          Also update
  ------------------------------  ------------------------------------------
  New DB table (historical data)  schema.py list_tables(), archive_registry.py,
                                  merge script uses registry automatically,
                                  readmefirst Section 7 if special case
  New log table                   RETENTION_CONFIG, weekly_log_cleanup in
                                  scheduler.py, readmefirst Section 5
  New scheduler job               config.py SCHEDULER_CONFIG, scheduler.py
                                  JOB_FUNCS, dashboard/server.py job metadata,
                                  readmefirst Section 5, OPERATIONS.md
  New deploy/azure script         setup-new-environment.ps1 if user-facing,
                                  readmefirst Section 4, deploy/README.md,
                                  setup-manifest.json if verifiable
  New laptop automation           register-*.ps1, setup-laptop.ps1,
                                  setup-manifest.json, readmefirst Section 5


================================================================================
SECTION 9 — TROUBLESHOOTING
================================================================================

  .\deploy\azure\Test-EnvironmentSetup.ps1
  python scripts/validate_setup_sync.py

  SSH fails          -> VM running? VmHost + .pem in laptop.config.ps1?
  Dashboard down     -> open-port-5001.ps1, VM uptime schedule
  Archive not merging-> register-laptop-archive-task.ps1, VM git pull + vm-restart
  Zerodha execute    -> OPT_DASHBOARD_API_KEY in .env.docker on VM


================================================================================
SECTION 10 — DOC INDEX
================================================================================

  readmefirst.txt                 THIS FILE — start here
  deploy/azure/MAINTAIN-SETUP.md  Developer checklist when adding infra/tables
  deploy/azure/SETUP-CHECKLIST.md Human-readable setup steps
  deploy/azure/ARCHIVE-AUTOMATION.md  Archive pipeline detail
  deploy/azure/OPERATIONS.md      Day-2 ops: deploy code, backup, restore
  deploy/azure/AI-BOOTSTRAP.md    Extended AI notes (optional)
  deploy/README.md                Script index
  ARCHITECTURE.txt                Module boundaries

================================================================================
