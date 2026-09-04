# AI bootstrap — fresh laptop + fresh VM

> **Superseded by repo root [`readmefirst.txt`](../../readmefirst.txt)** — use that file.
> AI agents: execute readmefirst.txt Section 3; do not require the user to paste this file.

## Scenario A — Brand new (empty VM, empty DB)

### You do first (human only — ~15 min)

| # | Action |
|---|--------|
| 1 | Clone repo: `git clone https://github.com/tamajit20/options_advisor_system.git` |
| 2 | Azure Portal → create Ubuntu 22.04 VM (Standard_B2s), SSH key → save `.pem` |
| 3 | Note VM **public IP**, resource group name, VM name |
| 4 | Install on laptop: **Git**, **Python 3**, **Azure CLI** (`winget install Microsoft.AzureCLI`), **OpenSSH** |
| 5 | Install **SQL Server Express** + SSMS (for local archive DB + sqlcmd) |
| 6 | Have ready: strong DB password, Zerodha API key/secret, random `OPT_DASHBOARD_API_KEY` |

### AI / scripts do the rest

```powershell
cd options_advisor_system
copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
copy .env.docker.example .env.docker
# AI fills laptop.config.ps1 + .env.docker with your values
az login
.\deploy\azure\setup-new-environment.ps1
.\deploy\azure\Test-EnvironmentSetup.ps1
```

---

## Scenario B — New laptop, keep existing data

You also need:

- Old **`OptionsAdvisorDB-*.bak`** (hot DB backup) and/or  
- Old **`D:\Backups\OptionsAdvisorDB\archive\`** folder (weekly archive chunks)  
- Old **`.env.docker`** secrets (passwords, Zerodha keys) — never commit to git

```powershell
.\deploy\azure\setup-new-environment.ps1 -RestoreFromBackup "D:\Backups\OptionsAdvisorDB\OptionsAdvisorDB-YYYYMMDD.bak"
```

After first archive merge runs, cumulative history lives in local **`OptionsAdvisorDB_Archive`**.

---

## Scenario C — New laptop only (VM unchanged)

```powershell
copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
# edit VmHost, SshKeyPath, LocalSqlServer
.\deploy\azure\setup-laptop.ps1
.\deploy\azure\Test-EnvironmentSetup.ps1
```

---

## Config files the AI must create (never commit secrets)

| File | Purpose |
|------|---------|
| `deploy/azure/laptop.config.ps1` | VM IP, SSH key, Azure RG, local SQL instance, backup paths |
| `.env.docker` | DB passwords, Zerodha, dashboard API key — uploaded to VM |

Templates: `laptop.config.ps1.example`, `.env.docker.example`

---

## What becomes automated (no daily action)

| Layer | Automation |
|-------|------------|
| **VM** Mon–Fri 08:55–15:45 | Azure Automation start/stop (`VMUpTimeConfiguration.ps1`) |
| **VM** Fri 09:30 | `weekly_archive` — hot → `*_Archive` |
| **VM** Fri 09:35 | `weekly_log_cleanup` — logs only |
| **VM** Fri 15:36 | `archive_export` — `.bak` for laptop |
| **VM** Fri 15:38 | `db_backup` — hot DB snapshot |
| **Laptop** Mon–Fri 09:15 | Task `OptionsAdvisor-ArchiveMerge` — pull, merge, VM ack |

---

## Manual forever (trading morning)

- **Zerodha login** once per market day (dashboard 🔑 or `python main.py --zerodha-login` on VM)

---

## Verify nothing missed

```powershell
.\deploy\azure\Test-EnvironmentSetup.ps1
```

All critical lines must show `[OK]`.

---

## Key doc map for AI

| Path | Content |
|------|---------|
| `deploy/azure/setup-new-environment.ps1` | One-shot greenfield |
| `deploy/azure/setup-laptop.ps1` | Laptop-only |
| `deploy/azure/Test-EnvironmentSetup.ps1` | Checklist runner |
| `deploy/azure/setup-manifest.json` | Machine-readable checks |
| `deploy/azure/remote-vm-install.ps1` | VM Docker + app |
| `deploy/azure/VMUpTimeConfiguration.ps1` | VM schedule |
| `deploy/azure/restore-database-from-laptop.ps1` | Laptop .bak → VM |
| `deploy/azure/backup-database-to-laptop.ps1` | VM .bak → laptop |
| `deploy/azure/pull-archive-and-merge.ps1` | Archive merge |
| `deploy/vm-restart.sh` | Redeploy app on VM after git pull |
| `config.py` | Scheduler jobs, retention, archive export |

---

## Shorter prompt (if context is limited)

```
Read deploy/azure/AI-BOOTSTRAP.md and deploy/azure/SETUP-CHECKLIST.md. Bootstrap this repo on my Windows laptop: create laptop.config.ps1 and .env.docker from examples (ask me for secrets), run setup-new-environment.ps1, restore DB if I provide a .bak path, register archive scheduled task, run Test-EnvironmentSetup.ps1 until critical checks pass. Deploy latest code to VM via SSH. Tell me what's left manual.
```
