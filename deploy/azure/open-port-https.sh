#!/usr/bin/env bash
# Open TCP 80 + 443 for Caddy HTTPS (Let's Encrypt needs 80 for HTTP-01).
#
# Usage (on VM):
#   ./deploy/azure/open-port-https.sh
#   AZURE_NSG_SOURCE=1.2.3.4/32 ./deploy/azure/open-port-https.sh
#
# From Windows laptop:
#   .\deploy\azure\open-port-https.ps1
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

SOURCE="${AZURE_NSG_SOURCE:-*}"
PRIORITY_BASE="${AZURE_NSG_HTTPS_PRIORITY:-1020}"

_is_azure() {
  curl -sf -H Metadata:true --max-time 2 \
    "http://169.254.169.254/metadata/instance?api-version=2021-02-01" >/dev/null 2>&1
}

_azure_meta() {
  curl -sf -H Metadata:true --max-time 2 \
    "http://169.254.169.254/metadata/instance/compute/$1?api-version=2021-02-01&format=text"
}

_open_ufw_port() {
  local port="$1"
  if command -v ufw >/dev/null 2>&1; then
    if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
      if sudo ufw status numbered 2>/dev/null | grep -q "${port}/tcp"; then
        echo "    ufw: ${port}/tcp already allowed"
      else
        sudo ufw allow "${port}/tcp" comment "Options Advisor HTTPS"
        echo "    ufw: allowed ${port}/tcp"
      fi
    fi
  fi
}

_find_nsg() {
  local rg="$1" vm="$2"
  local nic_id nsg_id subnet_id
  nic_id=$(az vm show -g "$rg" -n "$vm" --query "networkProfile.networkInterfaces[0].id" -o tsv)
  nsg_id=$(az network nic show --ids "$nic_id" --query "networkSecurityGroup.id" -o tsv 2>/dev/null || true)
  if [[ -n "$nsg_id" && "$nsg_id" != "null" ]]; then
    echo "$nsg_id"
    return 0
  fi
  subnet_id=$(az network nic show --ids "$nic_id" --query "ipConfigurations[0].subnet.id" -o tsv)
  nsg_id=$(az network vnet subnet show --ids "$subnet_id" --query "networkSecurityGroup.id" -o tsv 2>/dev/null || true)
  if [[ -n "$nsg_id" && "$nsg_id" != "null" ]]; then
    echo "$nsg_id"
    return 0
  fi
  return 1
}

_open_nsg_port() {
  local port="$1" priority="$2"
  local rule_name="Allow-OptionsAdvisor-${port}"
  local rg vm nsg_id nsg_name rg_from_nsg

  rg="$(_azure_meta resourceGroupName)"
  vm="$(_azure_meta name)"
  nsg_id=$(_find_nsg "$rg" "$vm") || return 1
  nsg_name=$(basename "$nsg_id")
  rg_from_nsg=$(az network nsg show --ids "$nsg_id" --query "resourceGroup" -o tsv)

  if az network nsg rule show -g "$rg_from_nsg" --nsg-name "$nsg_name" -n "$rule_name" >/dev/null 2>&1; then
    echo "    Azure NSG: rule '$rule_name' already exists"
    return 0
  fi

  az network nsg rule create \
    -g "$rg_from_nsg" \
    --nsg-name "$nsg_name" \
    -n "$rule_name" \
    --priority "$priority" \
    --source-address-prefixes "$SOURCE" \
    --source-port-ranges '*' \
    --destination-address-prefixes '*' \
    --destination-port-ranges "$port" \
    --access Allow \
    --protocol Tcp \
    --description "Options Advisor HTTPS (Caddy)"

  echo "    Azure NSG: opened TCP ${port} on ${nsg_name}"
}

echo "==> Opening HTTPS ports 80 and 443..."
_open_ufw_port 80
_open_ufw_port 443

if ! _is_azure; then
  echo "    Not Azure (or no metadata). Ensure cloud firewall allows 80/443."
  exit 0
fi

if ! command -v az >/dev/null 2>&1; then
  echo "WARNING: az CLI missing — open 80/443 from laptop: .\\deploy\\azure\\open-port-https.ps1"
  exit 1
fi
if ! az account show >/dev/null 2>&1; then
  az login --identity >/dev/null 2>&1 || true
fi
if ! az account show >/dev/null 2>&1; then
  echo "WARNING: Not logged in to Azure — open 80/443 from laptop:"
  echo "         .\\deploy\\azure\\open-port-https.ps1"
  exit 1
fi

_open_nsg_port 80 "$PRIORITY_BASE"
_open_nsg_port 443 "$((PRIORITY_BASE + 1))"
echo "==> Done. HTTPS ports ready for Caddy / Let's Encrypt."
