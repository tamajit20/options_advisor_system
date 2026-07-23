# Deploy

**Azure runbook (first install · DB update · code deploy):** [azure/OPERATIONS.md](azure/OPERATIONS.md)

## Scripts

| Script | Run on | Purpose |
|--------|--------|---------|
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
