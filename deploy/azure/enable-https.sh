#!/usr/bin/env bash
# Enable HTTPS for the dashboard via Caddy (free, same VM).
#
# Default: self-signed cert on the VM public IP — NO domain needed.
#   Browser warns once → Advanced → Proceed. Still real HTTPS (encrypted).
#
# Optional trusted cert (no domain purchase): use free sslip.io DNS
#   HTTPS_MODE=acme
#   HTTPS_DOMAIN=52.230.104.81.sslip.io   # replace with your public IP
#   CADDY_ACME_EMAIL=you@example.com
#
# Or your own DNS name with HTTPS_MODE=acme.
#
# Usage (on the VM):
#   cd ~/options_advisor_system
#   ./deploy/azure/enable-https.sh              # self-signed on IP
#   HTTPS_MODE=acme ./deploy/azure/enable-https.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ ! -f .env.docker ]]; then
  echo "ERROR: .env.docker missing — copy from .env.docker.example first."
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env.docker
set +a

MODE="${HTTPS_MODE:-selfsigned}"
DOMAIN="${HTTPS_DOMAIN:-}"
EMAIL="${CADDY_ACME_EMAIL:-}"
PUBLIC="${OPT_PUBLIC_BASE_URL:-}"

_detect_public_ip() {
  curl -sf -H Metadata:true --max-time 2 \
    "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" \
    2>/dev/null \
    || curl -sf --max-time 3 https://api.ipify.org 2>/dev/null \
    || hostname -I 2>/dev/null | awk '{print $1}'
}

_upsert_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env.docker 2>/dev/null; then
    sed -i.bak "s|^${key}=.*|${key}=${val}|" .env.docker
  else
    echo "${key}=${val}" >> .env.docker
  fi
}

PUBLIC_IP="${FORCE_PUBLIC_IP:-}"
PUBLIC_IP="${PUBLIC_IP:-$(_detect_public_ip || true)}"
PUBLIC_IP="${PUBLIC_IP//$'\r'/}"
PUBLIC_IP="${PUBLIC_IP// /}"

case "$MODE" in
  selfsigned|internal|ip)
    MODE=selfsigned
    if [[ -z "$PUBLIC_IP" ]]; then
      echo "ERROR: Could not detect public IP for self-signed cert. Set OPT_PUBLIC_BASE_URL=https://YOUR_IP"
      exit 1
    fi
    mkdir -p deploy/caddy/certs
    CERT=deploy/caddy/certs/cert.pem
    KEY=deploy/caddy/certs/key.pem
    if [[ ! -f "$CERT" || ! -f "$KEY" || ! -f deploy/caddy/certs/.for_ip || "$(cat deploy/caddy/certs/.for_ip 2>/dev/null)" != "$PUBLIC_IP" ]]; then
      echo "==> Generating self-signed cert for IP ${PUBLIC_IP} (825 days)..."
      openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
        -keyout "$KEY" -out "$CERT" \
        -subj "/CN=${PUBLIC_IP}" \
        -addext "subjectAltName=IP:${PUBLIC_IP},DNS:localhost" \
        2>/dev/null \
      || openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
        -keyout "$KEY" -out "$CERT" \
        -subj "/CN=${PUBLIC_IP}"
      echo "$PUBLIC_IP" > deploy/caddy/certs/.for_ip
      chmod 644 "$CERT"
      chmod 600 "$KEY"
    else
      echo "==> Reusing existing self-signed cert in deploy/caddy/certs/"
    fi
    cp -f deploy/caddy/Caddyfile.selfsigned deploy/caddy/Caddyfile
    if [[ -z "$PUBLIC" ]]; then
      PUBLIC="https://${PUBLIC_IP}"
      _upsert_env OPT_PUBLIC_BASE_URL "$PUBLIC"
      echo "==> Set OPT_PUBLIC_BASE_URL=${PUBLIC}"
    fi
    ACCESS_URL="${PUBLIC:-https://${PUBLIC_IP}}"
    ;;
  acme|letsencrypt)
    MODE=acme
    if [[ -z "$DOMAIN" ]]; then
      if [[ -n "$PUBLIC_IP" ]]; then
        DOMAIN="${PUBLIC_IP}.sslip.io"
        _upsert_env HTTPS_DOMAIN "$DOMAIN"
        echo "==> No HTTPS_DOMAIN set — using free sslip.io name: ${DOMAIN}"
      else
        echo "ERROR: HTTPS_MODE=acme needs HTTPS_DOMAIN (or detectable public IP for sslip.io)."
        exit 1
      fi
    fi
    if [[ "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      DOMAIN="${DOMAIN}.sslip.io"
      _upsert_env HTTPS_DOMAIN "$DOMAIN"
      echo "==> Bare IP → using ${DOMAIN} (free DNS, trusted cert)"
    fi
    if [[ -z "$EMAIL" || "$EMAIL" == "admin@localhost" ]]; then
      EMAIL="admin@${DOMAIN}"
      _upsert_env CADDY_ACME_EMAIL "$EMAIL"
      echo "==> Set CADDY_ACME_EMAIL=${EMAIL}"
    fi
    if [[ -z "$PUBLIC" ]]; then
      PUBLIC="https://${DOMAIN}"
      _upsert_env OPT_PUBLIC_BASE_URL "$PUBLIC"
      echo "==> Set OPT_PUBLIC_BASE_URL=${PUBLIC}"
    fi
    # ACME mode still needs placeholder cert files if volume is mounted empty —
    # use dummy files only when missing so compose mount succeeds; Caddyfile.acme ignores them.
    mkdir -p deploy/caddy/certs
    if [[ ! -f deploy/caddy/certs/cert.pem ]]; then
      openssl req -x509 -newkey rsa:2048 -days 1 -nodes \
        -keyout deploy/caddy/certs/key.pem -out deploy/caddy/certs/cert.pem \
        -subj "/CN=placeholder" 2>/dev/null || true
    fi
    cp -f deploy/caddy/Caddyfile.acme deploy/caddy/Caddyfile
    ACCESS_URL="https://${DOMAIN}"
    ;;
  *)
    echo "ERROR: HTTPS_MODE must be selfsigned or acme (got: $MODE)"
    exit 1
    ;;
esac

_upsert_env HTTPS_MODE "$MODE"

export COMPOSE_PROFILES="${COMPOSE_PROFILES:-bundled}"
case ",${COMPOSE_PROFILES}," in
  *,https,*) ;;
  *)
    export COMPOSE_PROFILES="${COMPOSE_PROFILES},https"
    echo "==> Adding https to COMPOSE_PROFILES → ${COMPOSE_PROFILES}"
    _upsert_env COMPOSE_PROFILES "$COMPOSE_PROFILES"
    ;;
esac

# Re-load after upserts so compose sees fresh values
set -a
# shellcheck disable=SC1091
. ./.env.docker
set +a
export COMPOSE_PROFILES

echo "==> Opening NSG / firewall for 80 and 443 (best effort)..."
chmod +x deploy/azure/open-port-https.sh 2>/dev/null || true
./deploy/azure/open-port-https.sh || {
  echo "WARNING: Could not open 80/443 from the VM."
  echo "         From your laptop run: .\\deploy\\azure\\open-port-https.ps1"
}

echo "==> Starting Caddy (mode=${MODE})..."
# Keep Flask :5001 on localhost only — Caddy reaches it on the Docker network.
if [[ "$MODE" == "selfsigned" || "$MODE" == "acme" ]]; then
  _upsert_env OPT_DASHBOARD_PUBLISH "127.0.0.1:${OPT_DASHBOARD_PORT:-5001}:5001"
  set -a
  # shellcheck disable=SC1091
  . ./.env.docker
  set +a
  export COMPOSE_PROFILES
  docker compose up -d options_advisor
fi
docker compose up -d caddy

echo ""
echo "Done. Open:"
echo "  ${ACCESS_URL}"
if [[ "$MODE" == "selfsigned" ]]; then
  echo ""
  echo "Browser will warn (self-signed). Click Advanced → Proceed / Continue."
  echo "Traffic is still encrypted. For a trusted cert with no bought domain:"
  echo "  HTTPS_MODE=acme HTTPS_DOMAIN=${PUBLIC_IP:-YOUR_IP}.sslip.io ./deploy/azure/enable-https.sh"
fi
echo ""
echo "Zerodha Kite redirect URL:"
echo "  ${ACCESS_URL%/}/zerodha/callback"
echo ""
echo "Optional: restrict or close public :5001 in Azure NSG so only HTTPS is used."
docker compose ps caddy options_advisor 2>/dev/null || true
