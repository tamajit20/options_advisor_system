# Database backup & restore

## Bundled Docker SQL Server (Oracle Cloud / Linux VM)

Requires the stack running with the `bundled` profile.

### Backup

```bash
chmod +x deploy/backup.sh
./deploy/backup.sh
```

Creates `./backups/OptionsAdvisorDB-YYYYMMDD-HHMMSS.bak` (compressed).

Copy off the VM periodically (SCP, Object Storage, etc.):

```bash
scp ubuntu@<VM_IP>:~/options_advisor_system/backups/*.bak ./
```

### Restore

```bash
./deploy/restore.sh backups/OptionsAdvisorDB-20260712-120000.bak
```

Stops the app, restores with `REPLACE`, restarts.

### Manual one-liner (no script)

```bash
source .env.docker
export COMPOSE_PROFILES=bundled
docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -Q \
  "BACKUP DATABASE [OptionsAdvisorDB] TO DISK = N'/var/opt/mssql/backup/adhoc.bak' WITH INIT, COMPRESSION"
docker cp options_sqlserver:/var/opt/mssql/backup/adhoc.bak ./backups/
```

---

## Windows laptop (SQLEXPRESS on host)

### Option A — SSMS (easiest)

1. Open **SQL Server Management Studio**
2. Connect to `TAMAJITLAPTOP\SQLEXPRESS`
3. Right-click **OptionsAdvisorDB** → **Tasks** → **Back Up…**
4. Destination: `D:\Backups\OptionsAdvisorDB.bak` → OK

### Option B — sqlcmd in PowerShell

```powershell
sqlcmd -S "TAMAJITLAPTOP\SQLEXPRESS" -E -Q ^
  "BACKUP DATABASE [OptionsAdvisorDB] TO DISK = N'D:\Backups\OptionsAdvisorDB.bak' WITH INIT, COMPRESSION"
```

(`-E` = Windows auth; use `-U sa -P ...` if SQL auth.)

---

## Move laptop DB → Oracle bundled SQL

1. Backup on Windows (above) → `OptionsAdvisorDB.bak`
2. Copy to VM: `scp OptionsAdvisorDB.bak ubuntu@<VM_IP>:~/options_advisor_system/backups/`
3. On VM, after `./deploy/setup.sh` once (empty DB exists):

   ```bash
   ./deploy/restore.sh backups/OptionsAdvisorDB.bak
   ```

---

## Schedule automatic backups (optional, on VM)

Daily at 22:00 IST after EOD jobs:

```bash
crontab -e
```

Add:

```
0 22 * * 1-5 cd /home/ubuntu/options_advisor_system && ./deploy/backup.sh >> logs/backup.log 2>&1
```

Keep the last 7 `.bak` files; delete older ones to save disk.
