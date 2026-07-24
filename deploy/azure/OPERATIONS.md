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

Kite requires **HTTPS** for public redirect URLs. On Azure (HTTP + IP) use **manual paste**:

**One-time in [Kite Developer Console](https://developers.kite.trade):**

Redirect URL:

```
http://127.0.0.1:5001/zerodha/callback
```

**Each trading morning** (from your browser at `http://<VM_IP>:5001`):

1. Open **WS Monitor** tab → **Open Kite Login** (opens new tab).
2. Complete Kite login + 2FA.
3. Browser lands on `127.0.0.1` — copy the **full URL** from the address bar.
4. Back on the Azure dashboard → paste URL → **Submit Token**.

**Alternative via SSH:**

```bash
cd ~/options_advisor_system
set -a && source .env.docker && set +a && export COMPOSE_PROFILES=bundled
sg docker -c 'docker compose exec options_advisor python main.py --zerodha-login'
```

Paste the `request_token` when prompted.

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

## VM power schedule (cost saving)

Recommended **Mon–Fri only** (Sat/Sun off). All times **IST**.

| Session | Start VM | Stop VM | What runs |
|---------|----------|---------|-----------|
| **Market** | **08:55** | **15:40** | Zerodha login, intraday jobs, live suggestions, 15:35 snapshot |
| **EOD** | **20:30** | **21:00** | **EOD Nightly Pipeline** @ **20:35** (best-effort sequential bhav → IV → suggestion); **weekly_cleanup** @ **20:50** Fri |
| **Weekend** | off | off | — |
| **Mon only** | **08:55** | **15:40** | **events_seed** @ **09:00** (calendar sync) |

Stopping must **deallocate** the VM (not guest OS shutdown only) to save compute cost. You still pay for the OS disk and a static public IP if reserved.

**Azure Automation (UTC):** 08:55 IST = 03:25 UTC · 15:40 IST = 10:10 UTC · 20:30 IST = 15:00 UTC · 21:00 IST = 15:30 UTC.

### Automated setup (recommended — from Windows laptop)

Script: **`deploy/azure/VMUpTimeConfiguration.ps1`**

Defaults (override in `deploy/azure/laptop.config.ps1` or `deploy/azure/vm-uptime/vm-uptime.config.ps1`):

| Setting | Default |
|---------|---------|
| Resource group | `STOCKAPPS` |
| VM name | `OptionsAdvisor` |
| Automation account | `aa-stockapps-optionsadvisor` |

```powershell
az login

# Preview commands only
.\deploy\azure\VMUpTimeConfiguration.ps1 -WhatIf

# Create Automation account, runbooks, and Mon–Fri schedules
.\deploy\azure\VMUpTimeConfiguration.ps1

# First run: import Az modules into Automation (~15–30 min)
.\deploy\azure\VMUpTimeConfiguration.ps1 -ImportAzModules

# Remove schedules/runbooks (keeps Automation account)
.\deploy\azure\VMUpTimeConfiguration.ps1 -Remove
```

Runbooks live in `deploy/azure/vm-uptime/runbooks/`:

- `Start-OptionsAdvisorVm.ps1` — start VM
- `Stop-OptionsAdvisorVm.ps1` — stop + **deallocate**

Schedules created:

| Schedule name | UTC | IST | Action |
|---------------|-----|-----|--------|
| `sched-oa-start-market-mf` | 03:25 | 08:55 | Start |
| `sched-oa-stop-market-mf` | 10:10 | 15:40 | Stop |
| `sched-oa-start-eod-mf` | 15:00 | 20:30 | Start |
| `sched-oa-stop-eod-mf` | 15:30 | 21:00 | Stop |

**After setup — verify**

1. Azure Portal → **Automation account** → `aa-stockapps-optionsadvisor` → **Runbooks** → test **Start** then **Stop**
2. VM status must show **Stopped (deallocated)** when off
3. **Jobs** tab in Automation → last run **Completed**
4. Allow **~5 min** after start before cron jobs (SQL + Docker)

**Market-day checklist:** Zerodha login after **08:55** start; EOD pipeline auto-runs at **20:35** if VM started at **20:30**.

### Manual setup (Azure Portal)

If you prefer the Portal instead of the script:

1. **Create** → **Automation** → Automation account in **STOCKAPPS** (same region as VM)
2. **Identity** → System assigned → **On** → save
3. VM (or resource group) → **Access control (IAM)** → add **Virtual Machine Contributor** for the Automation account identity
4. **Runbooks** → create **Start-OptionsAdvisorVm** / **Stop-OptionsAdvisorVm** (copy from `deploy/azure/vm-uptime/runbooks/`)
5. **Schedules** → four weekly schedules (Mon–Fri, **UTC** times in table above)
6. Link each schedule to the matching runbook with parameters: `ResourceGroupName=STOCKAPPS`, `VMName=OptionsAdvisor`

---

The app uses **`eod_nightly_pipeline`**: one job at **20:35** runs all EOD steps back-to-back (~10–15 min).  
Each step is attempted even if upstream steps fail or bhav is late — independent jobs (VIX, FII, simulation) always run; downstream orchestrators skip gracefully when data is missing.  
Allow **5 min** after VM start for Docker/SQL before 20:35.

If **F&O bhav** is late (NSE not published by 20:35), the pipeline still runs VIX/FII/simulation and attempts IV/suggestion (they return 0 rows). Re-run **`fo_bhav_download`** from the Jobs tab when the file appears, then **`iv_calculation`** → **`suggestion_engine`** if needed.

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
| **VM start/stop schedules** | Laptop | `.\deploy\azure\VMUpTimeConfiguration.ps1` |
| Zerodha login | VM | `docker compose exec options_advisor python main.py --zerodha-login` |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `docker compose` can't see sqlserver | `export COMPOSE_PROFILES=bundled` (or re-login SSH) |
| Dashboard unreachable | Run `.\deploy\azure\open-port-5001.ps1` from laptop after `az login` |
| Restore fails | VM stack must be up: `docker compose ps` shows sqlserver **Up** |
| WS runner restarting | Run Zerodha login (Part 1 Step 4) |
| Kite rejects redirect URL (HTTPS required) | See **HTTPS for Zerodha OAuth** below |
| sqlcmd not found on laptop | Install SSMS / SQL Server tools, or use `-BackupPath` with existing `.bak` |

---

## HTTPS for Zerodha OAuth (Azure)

Kite Connect **requires HTTPS** for redirect URLs. The only HTTP exception is **`http://127.0.0.1`** (local dev).

`http://52.230.104.81:5001` will **not** be accepted in the Kite Developer Console.

### Option A — Domain + free SSL on the VM (recommended)

1. Point a domain (e.g. `options.yourdomain.com`) to the VM public IP in DNS.
2. Open Azure NSG ports **80** and **443** (same way you opened 5001).
3. Install Caddy on the VM — it obtains a Let's Encrypt certificate automatically:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile <<'EOF'
options.yourdomain.com {
    reverse_proxy localhost:5001
}
EOF
sudo systemctl reload caddy
```

4. In Kite Developer Console, set Redirect URL to:
   `https://options.yourdomain.com/zerodha/callback`
5. In VM `.env.docker`:
   ```env
   OPT_PUBLIC_BASE_URL=https://options.yourdomain.com
   ```
6. Restart app: `docker compose up -d options_advisor ws_runner`

Browse the dashboard at **`https://options.yourdomain.com`** (not the raw IP).

### Option B — Cloudflare Tunnel (HTTPS without opening 443 on Azure)

If your domain uses Cloudflare DNS, run `cloudflared tunnel` to expose `localhost:5001` on a stable `https://…` URL. See [Cloudflare Tunnel docs](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

Set `OPT_PUBLIC_BASE_URL` to the tunnel HTTPS URL and register the same `/zerodha/callback` path in Kite.

### Option C — Manual token paste (no HTTPS needed today)

Keep Kite Redirect URL as **`http://127.0.0.1:5001/zerodha/callback`** (allowed by Kite).

Each morning on the Azure dashboard:

1. Click **Open Kite Login** → complete login + 2FA.
2. Browser will land on `127.0.0.1` (may show an error page — that's OK).
3. Copy the **full URL** from the address bar (contains `request_token=…`).
4. Paste into the token box on the Azure dashboard → **Submit Token**.

Or use SSH: `docker compose exec options_advisor python main.py --zerodha-login` and paste the token when prompted.
