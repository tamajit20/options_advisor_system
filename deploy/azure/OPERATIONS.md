# Azure operations runbook

One place for the three things you do most often:

1. **First-time install** — new VM, app, and (optionally) your laptop database  
2. **Database update** — push a new `.bak` to Azure or pull one back  
3. **Code-only deploy** — latest git code, **no database change**

Scripts live in `deploy/` (VM) and `deploy/azure/` (Windows laptop).

---

## Part 1 — First-time install

Do this once when the Azure VM is new.

### Step 1 — Create the Azure VM (Azure Portal, manual)

1. [Azure Portal](https://portal.azure.com) → **Virtual machines** → **Create**
2. **Image:** Ubuntu 22.04 LTS  
3. **Size:** Standard_B2s (2 vCPU, 4 GB RAM) or larger  
4. **Authentication:** SSH public key  
5. **Networking:** allow inbound **SSH (22)** only — Portal does not list 5001 here; install script opens it (see Step 2b)  
6. Create → note the **Public IP** (e.g. `20.x.x.x`)

### Step 2 — Install app + SQL Server on the VM

**Recommended (from Windows laptop — includes opening port 5001):**

```powershell
copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
notepad deploy\azure\laptop.config.ps1   # VmHost, SshKeyPath
az login
.\deploy\azure\remote-vm-install.ps1
```

This SSHs to the VM, runs `vm-install-deploy.sh`, then opens NSG port **5001** from your laptop.

**Manual (SSH into VM only):**

SSH into the VM (use the `.pem` key from Azure VM create):

```bash
ssh -i D:/Share/StockAnalyzer/OptionsAdvisor_key.pem azureuser@52.230.104.81
```

Or with placeholder IP:

```bash
ssh -i /path/to/OptionsAdvisor_key.pem azureuser@<VM_PUBLIC_IP>
```

Run:

```bash
git clone -b master_zerodha https://github.com/tamajit20/options_advisor_system.git
cd options_advisor_system

cp .env.docker.example .env.docker
nano .env.docker
```

Set at minimum in `.env.docker`:

```env
MSSQL_SA_PASSWORD=YourStrong!Pass123
OPT_DB_PASSWORD=YourStrong!Pass123    # must match MSSQL_SA_PASSWORD
OPT_ZERODHA_API_KEY=your_key
OPT_ZERODHA_API_SECRET=your_secret
```

Deploy:

```bash
chmod +x deploy/vm-install-deploy.sh
./deploy/vm-install-deploy.sh
```

- First run creates an **empty** `OptionsAdvisorDB`.  
- Open dashboard: `http://<VM_PUBLIC_IP>:5001`

### Step 2b — Open port 5001 (dashboard)

`vm-install-deploy.sh` **step 5/5** calls `deploy/azure/open-port-5001.sh` automatically.

On Azure this usually **fails on the VM alone** (NSG needs `az login`). Use one of:

| Method | Command |
|--------|---------|
| **All-in-one (recommended)** | `.\deploy\azure\remote-vm-install.ps1` |
| **After manual VM install** | `az login` then `.\deploy\azure\open-port-5001.ps1` |

```powershell
az login
copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
notepad deploy\azure\laptop.config.ps1   # set VmHost = VM public IP
.\deploy\azure\open-port-5001.ps1
```

Optional — restrict to your home IP only:

```powershell
.\deploy\azure\open-port-5001.ps1 -SourceIp "YOUR.IP.ADDR/32"
```

**Or on the VM** (after `az login --use-device-code`):

```bash
./deploy/azure/open-port-5001.sh
```

### Step 3 (optional) — Copy your laptop database to Azure

Skip if you want a **fresh empty** database. Do this if you have trades on local SQLEXPRESS.

**On Windows laptop** (PowerShell, from repo folder):

```powershell
copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
notepad deploy\azure\laptop.config.ps1
```

Edit `laptop.config.ps1`:

- `VmHost` = your VM public IP  
- `VmUser` = `azureuser`  
- `LocalSqlServer` = `TAMAJITLAPTOP\SQLEXPRESS` (or your instance)

Restore (backs up laptop DB, uploads, restores on VM):

```powershell
.\deploy\azure\restore-database-from-laptop.ps1 -CreateLocalBackup
```

If you already ran Step 2 with an empty DB, that is fine — restore **overwrites** the VM database.

### Step 4 — Zerodha login (every trading morning)

On the VM:

```bash
cd ~/options_advisor_system
docker compose exec options_advisor python main.py --zerodha-login
```

Follow the URL, log in, paste the `request_token`.

---

## Part 2 — Database update (Azure)

Use when you only want to **change database data**, not application code.

### Option A — Laptop → Azure (replace VM database with laptop copy)

**When:** You traded locally and want Azure to match your laptop book.

**On Windows laptop:**

```powershell
cd D:\Share\StockAnalyzer\options_advisor_system   # or your clone path

# Ensure laptop.config.ps1 has correct VmHost
.\deploy\azure\restore-database-from-laptop.ps1 -CreateLocalBackup
```

Or from an existing backup file:

```powershell
.\deploy\azure\restore-database-from-laptop.ps1 -BackupPath "D:\Backups\OptionsAdvisorDB\OptionsAdvisorDB-20260723.bak"
```

**What it does:** `BACKUP DATABASE` on laptop (if `-CreateLocalBackup`) → upload `.bak` → `RESTORE DATABASE` on VM.  
**Does not change:** app code, logs, `data/zerodha_session.json` on VM.

---

### Option B — Azure → Laptop (download VM database)

**When:** You want a safety copy on your laptop.

**On Windows laptop:**

```powershell
.\deploy\azure\backup-database-to-laptop.ps1
```

File saved to `D:\Backups\OptionsAdvisorDB\` (or path in `laptop.config.ps1`).

---

### Option C — Backup / restore on VM only (no laptop)

**When:** Snapshot on VM before a risky change.

**On VM (SSH):**

```bash
cd ~/options_advisor_system
export COMPOSE_PROFILES=bundled

# Backup — .bak stays on VM in backups/
./deploy/backup.sh

# Restore — overwrites DB from a file already on VM
./deploy/restore.sh backups/OptionsAdvisorDB-YYYYMMDD-HHMMSS.bak
```

---

## Part 3 — Deploy latest code only (no database change)

Use after a git push when you want **new application code** but **keep the existing database and trades**.

### On the VM (SSH)

```bash
cd ~/options_advisor_system
export COMPOSE_PROFILES=bundled

git pull origin master_zerodha

docker compose build options_advisor
docker compose up -d
```

Check:

```bash
docker compose ps
docker compose logs -f options_advisor --tail 50
```

Dashboard: `http://<VM_PUBLIC_IP>:5001`

### Important

| Do | Don't |
|----|--------|
| `git pull` + `docker compose build` + `up -d` | Run `./deploy/vm-install-deploy.sh --fresh-db` |
| Keep existing DB volume | Run restore scripts unless you **intend** to replace data |
| Re-login Zerodha if token expired | Wipe `sqlserver_data` Docker volume |

### If `vm-install-deploy.sh` asks about the database

When re-running the full install script, choose:

- **`[2]` Keep existing** — safe for code redeploy with same data  
- **`[1]` Fresh** — **deletes all trades** (only when you want empty DB)

For code-only updates, prefer the **Part 3** commands above instead of re-running the full install script.

---

## Quick reference

| Goal | Where | Command |
|------|-------|---------|
| First install | VM | `./deploy/vm-install-deploy.sh` |
| Copy laptop DB → Azure | Laptop | `.\deploy\azure\restore-database-from-laptop.ps1 -CreateLocalBackup` |
| Copy Azure DB → laptop | Laptop | `.\deploy\azure\backup-database-to-laptop.ps1` |
| Backup on VM only | VM | `./deploy/backup.sh` |
| Restore on VM only | VM | `./deploy/restore.sh backups/file.bak` |
| **Latest code, same DB** | VM | `git pull && docker compose build options_advisor && docker compose up -d` |
| Open dashboard port 5001 | Laptop | `.\deploy\azure\open-port-5001.ps1` |
| Zerodha login | VM | `docker compose exec options_advisor python main.py --zerodha-login` |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `docker compose` can't see sqlserver | `export COMPOSE_PROFILES=bundled` (or re-login SSH) |
| Dashboard unreachable | Run `.\deploy\azure\open-port-5001.ps1` from laptop after `az login` |
| Restore fails | VM stack must be up: `docker compose ps` shows sqlserver **Up** |
| WS runner restarting | Run Zerodha login (Part 1 Step 4) |
| sqlcmd not found on laptop | Install SSMS / SQL Server tools, or use `-BackupPath` with existing `.bak` |
