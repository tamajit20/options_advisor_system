# Archive automation (VM + laptop)

VM uptime: **Mon–Fri 08:55–15:45 IST** (off weekends).

## VM (automatic — scheduler)

| When (Fri) | Job | What |
|------------|-----|------|
| 09:30 | `weekly_archive` | Copy old rows → `*_Archive` (hot rows stay) |
| 09:35 | `weekly_log_cleanup` | Delete logs and alerts |
| 15:36 | `archive_export` | `.bak` chunk + `PENDING.json` |
| 15:38 | `db_backup` | Single `backups/OptionsAdvisorDB-latest.bak` + `LAST_HOT_BACKUP.json` |

`db_backup` and `archive_export` run **inside** `options_advisor` via pyodbc (`lifecycle/sql_backup.py`). They do not call `docker compose` — the app image has no Docker CLI. SQL Server writes `.bak` files to `/var/opt/mssql/host-backups`, bind-mounted to `./backups` on the VM (also mounted on the app as `/app/backups`).

`deploy/backup.sh` remains a **host** helper (SSH / laptop pull). After compose has the bind-mount, it writes the same `./backups` folder.

## Laptop (one-time setup)

```powershell
cd D:\Share\StockAnalyzer\options_advisor_system
.\deploy\azure\register-laptop-archive-task.ps1
```

Task runs **Mon–Fri 09:15** (after VM is up; Monday catch-up if Friday’s laptop was off):

1. Download Friday's **hot** `OptionsAdvisorDB-latest.bak` onto the laptop (required).  
2. Download `PENDING.json` + archive `.bak` and merge into **`OptionsAdvisorDB_Archive`**.  
3. SSH ACK → VM deletes those hot rows, truncates `*_Archive`, deletes `PENDING.json` and export `.bak` files.  
   ACK **refuses** (VM files stay) if: the laptop never ran this script, `LAST_HOT_BACKUP.json` is missing/older than the export, or `*_Archive` grew after the export.

If the laptop is off at 09:15, nothing is deleted on the VM. The task uses `StartWhenAvailable`; the next weekday the laptop is on, it pulls then ACKs.

If merge succeeded but ACK was refused, the next weekday retries ACK only (does not re-merge the same chunk). Friday `archive_export` **does not** delete last week's export `.bak` while `PENDING.json` is still waiting.

Manual test: `.\deploy\azure\pull-archive-and-merge.ps1`

Hot DB copy (separate, on demand): `.\deploy\azure\backup-database-to-laptop.ps1`

VM disk: Friday `db_backup` writes `backups/OptionsAdvisorDB-latest.bak` (replaced each Friday **in place**). Archive export chunks stay on the VM until laptop ACK. After ACK, export `.bak` files are deleted; the hot `.bak` remains until the next Friday overwrite.

## Local query

SSMS → `OptionsAdvisorDB_Archive` on your local SQL Express instance (all history).
