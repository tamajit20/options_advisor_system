#!/usr/bin/env bash
# Open the dashboard port (default 5001) for browser access.
#
# On Azure: updates the Network Security Group (NSG) when Azure CLI is logged in.
# Also opens the port in ufw if ufw is active on the VM.
#
# Usage (on VM, from repo root):
#   ./deploy/azure/open-port-5001.sh
#   AZURE_NSG_SOURCE=1.2.3.4/32 ./deploy/azure/open-port-5001.sh   # restrict to your IP
#
# If Azure CLI is not logged in on the VM, run from Windows laptop instead:
#   .\deploy\azure\open-port-5001.ps1
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

PORT="${OPT_DASHBOARD_PORT:-5001}"
if [[ -f .env.docker ]]; then
  # shellcheck disable=SC1091
  source .env.docker
  PORT="${OPT_DASHBOARD_PORT:-5001}"
fi

RULE_NAME="Allow-OptionsAdvisor-${PORT}"
PRIORITY="${AZURE_NSG_RULE_PRIORITY:-1010}"
SOURCE="${AZURE_NSG_SOURCE:-*}"

_azure_public_ip() {
  curl -sf -H Metadata:true --max-time 2 \
    "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" \
    2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'
}

_is_azure() {
  curl -sf -H Metadata:true --max-time 2 \
    "http://169.254.169.254/metadata/instance?api-version=2021-02-01" >/dev/null 2>&1
}

_azure_meta() {
  curl -sf -H Metadata:true --max-time 2 \
    "http://169.254.169.254/metadata/instance/compute/$1?api-version=2021-02-01&format=text"
}

_open_ufw() {
  if command -v ufw >/dev/null 2>&1; then
    if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
      if sudo ufw status numbered 2>/dev/null | grep -q "${PORT}/tcp"; then
        echo "    ufw: ${PORT}/tcp already allowed"
      else
        sudo ufw allow "${PORT}/tcp" comment "Options Advisor dashboard"
        echo "    ufw: allowed ${PORT}/tcp"
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

_open_azure_nsg() {
  if ! command -v az >/dev/null 2>&1; then
    return 1
  fi
  if ! az account show >/dev/null 2>&1; then
    return 1
  fi

  local rg vm nsg_id nsg_name rg_from_nsg
  rg="$(_azure_meta resourceGroupName)"
  vm="$(_azure_meta name)"

  nsg_id=$(_find_nsg "$rg" "$vm") || return 1
  nsg_name=$(basename "$nsg_id")
  rg_from_nsg=$(az network nsg show --ids "$nsg_id" --query "resourceGroup" -o tsv)

  if az network nsg rule show -g "$rg_from_nsg" --nsg-name "$nsg_name" -n "$RULE_NAME" >/dev/null 2>&1; then
    echo "    Azure NSG: rule '$RULE_NAME' already exists on $nsg_name"
    return 0
  fi

  az network nsg rule create \
    -g "$rg_from_nsg" \
    --nsg-name "$nsg_name" \
    -n "$RULE_NAME" \
    --priority "$PRIORITY" \
    --source-address-prefixes "$SOURCE" \
    --source-port-ranges '*' \
    --destination-address-prefixes '*' \
    --destination-port-ranges "$PORT" \
    --access Allow \
    --protocol Tcp \
    --description "Options Advisor dashboard"

  echo "    Azure NSG: opened TCP ${PORT} on ${nsg_name} (source: ${SOURCE})"
}

echo "==> Opening dashboard port ${PORT}..."

_open_ufw

if _is_azure; then
  if _open_azure_nsg; then
    echo "==> Done. Dashboard: http://$(_azure_public_ip):${PORT}"
    exit 0
  fi

  echo ""
  echo "-----------------------------------------------------------------"
  echo " Azure VM detected — NSG port ${PORT} was NOT opened automatically."
  echo " Azure Portal only offers ports 22/80/443 at create time."
  echo ""
  echo " Easiest fix FROM YOUR WINDOWS LAPTOP:"
  echo "   az login"
  echo "   .\\deploy\\azure\\open-port-5001.ps1"
  echo ""
  echo " Or ON THIS VM (one-time Azure CLI login):"
  echo "   curl -sL https://aka.ms/InstallAzureCli | sudo bash"
  echo "   az login --use-device-code"
  echo "   ./deploy/azure/open-port-5001.sh"
  echo "-----------------------------------------------------------------"
else
  echo "    Not running on Azure (or metadata unavailable)."
  echo "    Ensure your cloud firewall allows inbound TCP ${PORT}."
fi
