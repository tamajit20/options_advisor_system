# Open dashboard port 5001 on Azure NSG — run from Windows laptop after VM create.
#
# Azure Portal only lets you pick SSH/80/443 at create time; this adds 5001.
#
# Prerequisites:
#   winget install Microsoft.AzureCLI   (or install Azure CLI)
#   az login
#
# Usage (from repo root):
#   .\deploy\azure\open-port-5001.ps1
#   .\deploy\azure\open-port-5001.ps1 -SourceIp "1.2.3.4/32"   # restrict to your IP
#
param(
    [string]$VmHost,
    [string]$Port = "5001",
    [string]$SourceIp,          # default: * (any). Pass your IP/32 for security.
    [string]$AzureResourceGroup,
    [string]$AzureVmName,
    [int]$Priority = 1010
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"
if (Test-Path $ConfigFile) { . $ConfigFile }

if (-not $VmHost) { $VmHost = $script:VmHost }
if (-not $AzureResourceGroup) { $AzureResourceGroup = $script:AzureResourceGroup }
if (-not $AzureVmName) { $AzureVmName = $script:AzureVmName }
if (-not $SourceIp) { $SourceIp = $(if ($script:AzureNsgSource) { $script:AzureNsgSource } else { "*" }) }

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI not found. Install: winget install Microsoft.AzureCLI"
}

$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    throw "Not logged in to Azure. Run: az login"
}

# Resolve VM by public IP if RG/name not in config
if (-not $AzureResourceGroup -or -not $AzureVmName) {
    if (-not $VmHost -or $VmHost -eq "YOUR_VM_PUBLIC_IP") {
        throw "Set VmHost (public IP) in deploy\azure\laptop.config.ps1, or pass -AzureResourceGroup and -AzureVmName"
    }
    Write-Host "==> Looking up VM with public IP $VmHost..."
    $vmJson = az vm list -d --query "[?publicIps=='$VmHost'] | [0]" -o json | ConvertFrom-Json
    if (-not $vmJson) {
        throw "No VM found with public IP $VmHost. Set AzureResourceGroup and AzureVmName in laptop.config.ps1"
    }
    $AzureResourceGroup = $vmJson.resourceGroup
    $AzureVmName = $vmJson.name
    Write-Host "    Found: $AzureVmName in $AzureResourceGroup"
}

$ruleName = "Allow-OptionsAdvisor-$Port"

Write-Host "==> Finding NSG for VM $AzureVmName..."
$nicId = az vm show -g $AzureResourceGroup -n $AzureVmName --query "networkProfile.networkInterfaces[0].id" -o tsv
$nsgId = az network nic show --ids $nicId --query "networkSecurityGroup.id" -o tsv 2>$null
if (-not $nsgId -or $nsgId -eq "null") {
    $subnetId = az network nic show --ids $nicId --query "ipConfigurations[0].subnet.id" -o tsv
    $nsgId = az network vnet subnet show --ids $subnetId --query "networkSecurityGroup.id" -o tsv
}
if (-not $nsgId -or $nsgId -eq "null") {
    throw "Could not find NSG on NIC or subnet. Add port $Port manually in Azure Portal → VM → Networking."
}

$nsgName = Split-Path $nsgId -Leaf
$nsgRg = (az network nsg show --ids $nsgId --query "resourceGroup" -o tsv)

$existing = az network nsg rule show -g $nsgRg --nsg-name $nsgName -n $ruleName 2>$null
if ($existing) {
    Write-Host "==> Rule '$ruleName' already exists on NSG '$nsgName'."
} else {
    Write-Host "==> Creating NSG rule on '$nsgName' (TCP $Port, source $SourceIp)..."
    az network nsg rule create `
        -g $nsgRg `
        --nsg-name $nsgName `
        -n $ruleName `
        --priority $Priority `
        --source-address-prefixes $SourceIp `
        --source-port-ranges "*" `
        --destination-address-prefixes "*" `
        --destination-port-ranges $Port `
        --access Allow `
        --protocol Tcp `
        --description "Options Advisor dashboard"
    Write-Host "    Rule created."
}

Write-Host ""
Write-Host "Done. Open dashboard: http://${VmHost}:${Port}"
