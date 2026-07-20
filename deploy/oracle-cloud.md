# Deploy on Oracle Cloud Free VM (all-in-one Docker)

One VM runs **SQL Server + options advisor + WS runner** via `docker compose`.

## 1. Create the VM

1. Sign up at [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/).
2. Create a VM (**Compute → Instances → Create**):
   - **Shape:** Ampere A1 (Always Free) — 2 OCPU, 12 GB RAM is plenty
   - **OS:** Ubuntu 22.04 or 24.04
   - **Region:** Mumbai (`ap-mumbai-1`) if available (closer to NSE)
   - Add your SSH public key
3. **Networking → Security list → Ingress:** allow TCP **22** (SSH) and **5001** (dashboard).
   Restrict 5001 to your home IP if possible, or use Tailscale instead of a public port.

## 2. Install Docker on the VM

```bash
ssh ubuntu@<VM_PUBLIC_IP>

sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"
# log out and back in so docker group applies
```

## 3. Deploy the app

```bash
git clone https://github.com/tamajit20/options_advisor_system.git
cd options_advisor_system

cp .env.docker.example .env.docker
nano .env.docker   # set MSSQL_SA_PASSWORD, OPT_DB_PASSWORD (same value), Zerodha API keys

chmod +x deploy/setup.sh
./deploy/setup.sh
```

That's it — `setup.sh` builds the image, starts SQL Server, runs `--init-db`, and brings up the full stack.

## 4. Daily Zerodha login (required for live data)

Kite tokens expire every trading day (~06:00 IST):

```bash
cd options_advisor_system
docker compose exec options_advisor python main.py --zerodha-login
```

Follow the URL, log in, paste the `request_token`. The session is saved in `data/zerodha_session.json`.

## 5. Useful commands

```bash
docker compose ps                          # all services healthy?
docker compose logs -f options_advisor     # scheduler / dashboard logs
docker compose logs sqlserver              # DB startup issues
docker compose exec options_advisor python main.py --check-db
docker compose restart                     # after config change
```

## 6. Migrate data from your laptop (optional)

If you have existing trades in local SQLEXPRESS, back up on Windows and restore into the container — or start fresh with `--init-db` for a clean paper book.

## Notes

- **SQL Server on ARM:** the official image is amd64; Docker runs it under emulation on Ampere VMs. First start can take ~60s; 2 OCPU is recommended.
- **DB persistence:** data lives in the Docker volume `sqlserver_data`. Back it up before deleting the VM.
- **Security:** do not commit `.env.docker` (gitignored). Use strong SA passwords.
