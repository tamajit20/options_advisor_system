# Archive automation (VM + laptop)

VM uptime: **Mon–Fri 08:55–15:45 IST** (off weekends).

## VM (automatic — scheduler)

| When (Fri) | Job | What |
|------------|-----|------|
| 09:30 | `weekly_archive` | Move old rows → `*_Archive` |
| 09:35 | `weekly_log_cleanup` | Delete logs only |
| 15:36 | `archive_export` | `.bak` chunk + `backups/archive/PENDING.json` |
| 15:38 | `db_backup` | Hot DB snapshot |

## Laptop (one-time setup)

```powershell
cd D:\Share\StockAnalyzer\options_advisor_system
.\deploy\azure\register-laptop-archive-task.ps1
```

Task runs **Mon–Fri 09:15** (after VM is up):

1. Download `PENDING.json` + `.bak` from VM  
2. Merge into **`OptionsAdvisorDB_Archive`** (cumulative — never replaces)  
3. SSH ACK → VM truncates `*_Archive`

Manual test: `.\deploy\azure\pull-archive-and-merge.ps1`

## Local query

SSMS → `OptionsAdvisorDB_Archive` on your local SQL Express instance (all history).
