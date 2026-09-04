# New laptop + new VM — setup checklist

Use **one command** for a greenfield install, then **verify** nothing was missed.

## Quick start (Windows laptop)

```powershell
cd D:\Share\StockAnalyzer\options_advisor_system

# 1. Config files (edit before continuing)
copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
copy .env.docker.example .env.docker
notepad deploy\azure\laptop.config.ps1   # VmHost, SshKeyPath, Azure RG, SQL instance
notepad .env.docker                      # DB passwords, Zerodha keys, OPT_DASHBOARD_API_KEY

# 2. Azure login
az login

# 3. Full setup (VM + laptop automation + uptime)
.\deploy\azure\setup-new-environment.ps1

# 4. Confirm
.\deploy\azure\Test-EnvironmentSetup.ps1
```

### Restore existing DB to new VM

```powershell
.\deploy\azure\setup-new-environment.ps1 -RestoreFromBackup "D:\Backups\OptionsAdvisorDB\OptionsAdvisorDB-20260904.bak"
```

### Laptop only (VM already running)

```powershell
.\deploy\azure\setup-laptop.ps1
```

---

## What each step configures

| Step | Script | What it does |
|------|--------|----------------|
| Laptop config | `setup-laptop.ps1` | `laptop.config.ps1`, backup folders, archive merge task |
| VM app + SQL | `remote-vm-install.ps1` | Clone/pull repo, Docker, `.env.docker`, port 5001 |
| VM uptime | `VMUpTimeConfiguration.ps1` | Start 08:55 / stop 15:45 Mon-Fri |
| DB (optional) | `restore-database-from-laptop.ps1` | Push `.bak` to VM |
| Verify | `Test-EnvironmentSetup.ps1` | SSH, dashboard, SQL, scheduled tasks, archive scripts |

---

## Manual checklist (if you prefer step-by-step)

### Azure Portal (once)

- [ ] Ubuntu 22.04 VM (e.g. Standard_B2s)
- [ ] SSH public key — save `.pem` on laptop
- [ ] Note public IP → `laptop.config.ps1` → `VmHost`
- [ ] NSG: SSH 22 (5001 opened by script)

### Laptop prerequisites

- [ ] Git, Python 3, OpenSSH, **sqlcmd** (SSMS), Azure CLI
- [ ] SQL Server Express (for `OptionsAdvisorDB_Archive`)
- [ ] `deploy/azure/laptop.config.ps1` filled in

### VM `.env.docker` minimum

```env
MSSQL_SA_PASSWORD=...
OPT_DB_PASSWORD=...          # same as MSSQL_SA_PASSWORD
OPT_ZERODHA_API_KEY=...
OPT_ZERODHA_API_SECRET=...
OPT_DASHBOARD_API_KEY=...    # required for Zerodha execute APIs
```

### After install

- [ ] Dashboard: `http://<VM_IP>:5001`
- [ ] Zerodha login (each trading morning)
- [ ] `Test-EnvironmentSetup.ps1` — all critical `[OK]`

---

## Automated weekly (no action needed)

| When | Where | Job |
|------|-------|-----|
| Fri 09:30 | VM | `weekly_archive` |
| Fri 09:35 | VM | `weekly_log_cleanup` |
| Fri 15:36 | VM | `archive_export` |
| Fri 15:38 | VM | `db_backup` |
| Mon-Fri 09:15 | Laptop | `OptionsAdvisor-ArchiveMerge` |

See [ARCHIVE-AUTOMATION.md](ARCHIVE-AUTOMATION.md).

---

## Troubleshooting

```powershell
.\deploy\azure\Test-EnvironmentSetup.ps1        # see what failed
.\deploy\azure\Test-EnvironmentSetup.ps1 -Strict  # include warnings
```

| Failure | Fix |
|---------|-----|
| SSH | Check `VmHost`, `.pem` path, VM running |
| Dashboard | `open-port-5001.ps1`, VM uptime schedule |
| sqlcmd | Install SSMS / SQL command tools |
| Archive task | `.\deploy\azure\register-laptop-archive-task.ps1` |
| VM archive scripts | On VM: `git pull && ./deploy/vm-restart.sh` |
