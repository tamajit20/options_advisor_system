#!/usr/bin/env bash
# Install Docker + deploy the full stack on a fresh Linux VM (Azure, Oracle, etc.).
#
# Installs SQL Server (Docker) and the application. On re-run, if the database
# already exists you will be asked:
#   [1] Delete and create fresh empty database
#   [2] Keep existing database and data (recommended after restore-from-laptop)
#
# Run ON the VM after SSH login:
#   chmod +x deploy/vm-install-deploy.sh && ./deploy/vm-install-deploy.sh
#
# Flags (passed through to deploy/setup.sh):
#   --fresh-db           wipe DB and recreate empty
#   --use-existing-db    keep DB data (schema upgrade only)
#
# First-time .env.docker:
#   cp .env.docker.example .env.docker
#   nano .env.docker   # MSSQL_SA_PASSWORD, OPT_DB_PASSWORD (same), Zerodha keys
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/tamajit20/options_advisor_system.git}"
REPO_BRANCH="${REPO_BRANCH:-master}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/options_advisor_system}"

_public_ip_hint() {
  curl -sf -H Metadata:true --max-time 2 \
    "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" \
    2>/dev/null \
    || hostname -I 2>/dev/null | awk '{print $1}' \
    || echo "<VmHost>"
}

# Re-exec under docker group if we were just added (avoids "log out and back in").
if ! docker info &>/dev/null 2>&1; then
  if groups 2>/dev/null | grep -qw docker; then
    exec sg docker -c "bash $(printf '%q ' "$0")$(printf '%q ' "$@")"
  fi
fi

echo "==> [1/4] Installing system packages (Docker, Git)..."
export DEBIAN_FRONTEND=noninteractive
if ! command -v docker &>/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y docker.io docker-compose-v2 git curl
  sudo usermod -aG docker "$USER" || true
  if ! docker info &>/dev/null 2>&1; then
    echo "    Docker installed; activating docker group for this session..."
    exec sg docker -c "bash $(printf '%q ' "$0")$(printf '%q ' "$@")"
  fi
else
  echo "    Docker already installed."
  sudo apt-get update -y
  sudo apt-get install -y docker-compose-v2 git curl 2>/dev/null || true
fi

# Azure VM: install Azure CLI so step 5 can update the NSG (needs az login or managed identity).
if curl -sf -H Metadata:true --max-time 2 \
    "http://169.254.169.254/metadata/instance?api-version=2021-02-01" >/dev/null 2>&1; then
  if ! command -v az &>/dev/null; then
    echo "    Azure VM detected — installing Azure CLI for NSG port 5001..."
    curl -sL https://aka.ms/InstallAzureCli | sudo bash
  fi
fi

echo "==> [2/4] Fetching application code..."
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  echo "    Repo exists at ${INSTALL_DIR} — pulling latest..."
  git -C "${INSTALL_DIR}" fetch origin "${REPO_BRANCH}"
  git -C "${INSTALL_DIR}" checkout "${REPO_BRANCH}"
  git -C "${INSTALL_DIR}" pull origin "${REPO_BRANCH}"
else
  echo "    Cloning ${REPO_URL} (${REPO_BRANCH})..."
  git clone -b "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"
chmod +x deploy/*.sh deploy/azure/open-port-5001.sh deploy/azure/open-port-https.sh deploy/azure/enable-https.sh 2>/dev/null || true

# Persist compose profiles for future shells (HTTPS added by enable-https / setup).
if ! grep -q 'COMPOSE_PROFILES=' "$HOME/.bashrc" 2>/dev/null; then
  echo 'export COMPOSE_PROFILES=bundled,https' >> "$HOME/.bashrc"
fi
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-bundled}"

echo "==> [3/4] Checking .env.docker..."
if [[ ! -f .env.docker ]]; then
  cp .env.docker.example .env.docker
  echo ""
  echo "ERROR: Created .env.docker from the example — you must edit it first."
  echo "  nano ${INSTALL_DIR}/.env.docker"
  echo ""
  echo "Set at minimum:"
  echo "  MSSQL_SA_PASSWORD=...   (strong password)"
  echo "  OPT_DB_PASSWORD=...     (must match MSSQL_SA_PASSWORD)"
  echo "  OPT_ZERODHA_API_KEY=..."
  echo "  OPT_ZERODHA_API_SECRET=..."
  echo ""
  echo "Then re-run:  ./deploy/vm-install-deploy.sh"
  exit 1
fi

# shellcheck disable=SC1091
set -a
source .env.docker
set +a
if [[ -z "${MSSQL_SA_PASSWORD:-}" || "${MSSQL_SA_PASSWORD}" == "ChangeMe!Str0ng#Pass" ]]; then
  echo "ERROR: Set a real MSSQL_SA_PASSWORD (and matching OPT_DB_PASSWORD) in .env.docker"
  exit 1
fi

echo "==> [4/5] Building and starting stack (SQL Server + app + WS runner)..."
echo "    SQL Server is installed via Docker on first run."
echo "    If a database already exists, you will be prompted: fresh vs keep existing."
./deploy/setup.sh "$@"

echo "==> [5/5] Opening HTTPS ports 80/443 (public :5001 not opened)..."
chmod +x deploy/azure/open-port-https.sh 2>/dev/null || true
./deploy/azure/open-port-https.sh || {
  echo "WARNING: Ports 80/443 were NOT opened in Azure NSG from the VM."
  echo "         Laptop: .\\deploy\\azure\\open-port-https.ps1"
}

# setup.sh already enables self-signed HTTPS; ensure bashrc matches .env.docker
if grep -q 'COMPOSE_PROFILES=.*https' .env.docker 2>/dev/null; then
  if grep -q 'COMPOSE_PROFILES=' "$HOME/.bashrc" 2>/dev/null; then
    sed -i.bak 's|^export COMPOSE_PROFILES=.*|export COMPOSE_PROFILES=bundled,https|' "$HOME/.bashrc" || true
  fi
fi

echo ""
echo "================================================================="
echo " Deploy complete."
echo " Dashboard HTTPS: https://$(_public_ip_hint)/   (accept browser cert warning once)"
echo " Public HTTP :5001 is not opened — use HTTPS only."
echo ""
echo " Restore database from laptop:"
echo "   .\\deploy\\azure\\restore-database-from-laptop.ps1"
echo ""
echo " Backup database to laptop:"
echo "   .\\deploy\\azure\\backup-database-to-laptop.ps1"
echo ""
echo " Each trading morning on the VM:"
echo "   docker compose exec options_advisor python main.py --zerodha-login"
echo ""
echo " New laptop setup (Windows):"
echo "   .\\deploy\\azure\\setup-new-environment.ps1"
echo "   .\\deploy\\azure\\Test-EnvironmentSetup.ps1"
echo ""
echo " Routine code deploy (schema auto on restart):"
echo "   ./deploy/update.sh"
echo "================================================================="
