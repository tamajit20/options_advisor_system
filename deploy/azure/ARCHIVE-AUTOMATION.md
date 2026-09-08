# Archive automation (VM + laptop)

VM uptime: **Mon–Fri 08:55–15:45 IST** (off weekends).

## VM (automatic — scheduler)

| When (Fri) | Job | What |
|------------|-----|------|
| 09:30 | `weekly_archive` | Move old rows → `*_Archive` |
| 09:35 | `weekly_log_cleanup` | Delete logs and alerts |
| 15:36 | `archive_export` | `.bak` chunk + `PENDING.json` (deleted after laptop ACK) |
| 15:38 | `db_backup` | Single `backups/OptionsAdvisorDB-latest.bak` (replaces last week) |

`db_backup` and `archive_export` run **inside** `options_advisor` via pyodbc (`lifecycle/sql_backup.py`). They do not call `docker compose` — the app image has no Docker CLI. SQL Server writes `.bak` files to `/var/opt/mssql/host-backups`, bind-mounted to `./backups` on the VM (also mounted on the app as `/app/backups`).

`deploy/backup.sh` remains a **host** helper (SSH / laptop pull). After compose has the bind-mount, it writes the same `./backups` folder.

## Laptop (one-time setup)

```powershell
cd D:\Share\StockAnalyzer\options_advisor_system
.\deploy\azure\register-laptop-archive-task.ps1
```

Task runs **Mon–Fri 09:15** (after VM is up; Monday catch-up if Friday’s laptop was off):

1. Download `PENDING.json` + `.bak` from VM  
2. Merge into **`OptionsAdvisorDB_Archive`** (cumulative — never replaces)  
3. SSH ACK → VM truncates `*_Archive`, deletes `PENDING.json` and the export `.bak`

If nothing is pending (no Friday archive rows, or `weekly_archive` has not run yet), the task exits 0.

Manual test: `.\deploy\azure\pull-archive-and-merge.ps1`

Hot DB copy (separate, on demand): `.\deploy\azure\backup-database-to-laptop.ps1`

VM disk: Friday `db_backup` writes a single `backups/OptionsAdvisorDB-latest.bak` (replaces last week). After a successful laptop pull, that file is deleted. Archive export keeps one pending `.bak` until the laptop ACK, then deletes it.

## Local query

SSMS → `OptionsAdvisorDB_Archive` on your local SQL Express instance (all history).
