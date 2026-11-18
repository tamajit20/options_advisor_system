#!/usr/bin/env bash
# Install/enable the systemd unit that starts the compose stack on boot.
# Usage (on VM): sudo ./deploy/install-boot-service.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$ROOT/deploy/options-advisor.service"
UNIT_DST=/etc/systemd/system/options-advisor.service
BOOT_SH="$ROOT/deploy/boot-stack.sh"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run as root (sudo $0)" >&2
  exit 1
fi
if [[ ! -f "$UNIT_SRC" || ! -f "$BOOT_SH" ]]; then
  echo "ERROR: missing $UNIT_SRC or $BOOT_SH" >&2
  exit 1
fi

chmod +x "$BOOT_SH" "$ROOT/deploy/load-compose-profiles.sh"
# Ensure docker + tailscale start on boot (idempotent).
systemctl enable docker.service 2>/dev/null || true
systemctl enable tailscaled.service 2>/dev/null || true

install -m 644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable options-advisor.service
# Start once now so we know the unit works without waiting for reboot.
systemctl restart options-advisor.service
systemctl --no-pager --full status options-advisor.service || true
echo "Installed: options-advisor.service (enabled on boot)"
