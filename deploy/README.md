# Deploy

**Fresh start with AI (Cursor):** say **Follow readmefirst.txt** — see repo root `readmefirst.txt`

**New laptop + new VM:** [azure/SETUP-CHECKLIST.md](azure/SETUP-CHECKLIST.md) — run `setup-new-environment.ps1` then `Test-EnvironmentSetup.ps1`

**Azure runbook (first install · DB update · code deploy):** [azure/OPERATIONS.md](azure/OPERATIONS.md)

## Scripts

| Script | Run on | Purpose |
|--------|--------|---------|
| `azure/setup-new-environment.ps1` | Windows | **Greenfield:** laptop + VM + uptime + verify |
| `azure/setup-laptop.ps1` | Windows | Laptop only: config, folders, archive task |
| `azure/Test-EnvironmentSetup.ps1` | Windows | Verify nothing missed (checklist) |
| `vm-install-deploy.sh` | Linux VM | Install Docker + deploy stack |
| `setup.sh` | VM | Build/start stack (used by vm-install-deploy) |
| `db-setup.sh` | VM | Fresh vs keep-existing database |
| `backup.sh` | VM | SQL BACKUP → `backups/*.bak` on VM |
| `restore.sh` | VM | RESTORE from `backups/*.bak` on VM |
| `azure/remote-vm-install.ps1` | Windows | Full VM install + open NSG 5001 |
| `azure/open-port-5001.ps1` | Windows | Open NSG port 5001 only |
| `azure/open-port-5001.sh` | VM | Same (needs `az login` on VM) |
| `azure/backup-database-to-laptop.ps1` | Windows | VM DB → laptop `.bak` |
| `azure/restore-database-from-laptop.ps1` | Windows | Laptop `.bak` → VM |
| `azure/register-laptop-archive-task.ps1` | Windows | Weekly archive merge task |
| `azure/pull-archive-and-merge.ps1` | Windows | Pull VM archive + merge locally |
