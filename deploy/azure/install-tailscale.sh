#!/usr/bin/env bash
# Join this VM to Tailscale. Auth key is a file path (deleted after read).
# Usage: ./deploy/azure/install-tailscale.sh /path/to/keyfile
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

KEY_FILE="${1:-}"
if [[ -z "$KEY_FILE" || ! -f "$KEY_FILE" ]]; then
  echo "ERROR: pass a Tailscale auth key file."
  exit 2
fi
KEY="$(tr -d '[:space:]' < "$KEY_FILE")"
rm -f "$KEY_FILE"
if [[ -z "$KEY" || "$KEY" == "tskey-auth-REPLACE_ME" ]]; then
  echo "ERROR: Tailscale auth key is empty."
  exit 2
fi

if ! command -v tailscale >/dev/null 2>&1; then
  echo "==> Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
fi

if command -v ufw >/dev/null 2>&1; then
  if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    sudo ufw allow in on tailscale0 >/dev/null 2>&1 || true
  fi
fi

echo "==> Joining tailnet as options-advisor..."
sudo tailscale up \
  --auth-key="$KEY" \
  --hostname=options-advisor \
  --accept-dns=false \
  --reset \
  --timeout=90s
unset KEY

IP="$(sudo tailscale ip -4 | head -n1)"
if [[ -z "$IP" ]]; then
  echo "ERROR: Tailscale did not assign an IPv4 address."
  exit 1
fi
mkdir -p data
printf '%s\n' "$IP" > data/tailscale_ipv4
chmod 644 data/tailscale_ipv4
echo "TAILSCALE_IP=$IP"
