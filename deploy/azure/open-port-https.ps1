# Open TCP 80 + 443 on Azure NSG for Caddy HTTPS (Let's Encrypt needs 80).
#
# Usage (from repo root, after az login):
#   .\deploy\azure\open-port-https.ps1
#   .\deploy\azure\open-port-https.ps1 -SourceIp "1.2.3.4/32"
#
param(
    [string]$VmHost,
    [string]$SourceIp,
    [string]$AzureResourceGroup,
    [string]$AzureVmName,
    [int]$PriorityBase = 1020
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
if (-not $account) { throw "Not logged in to Azure. Run: az login" }

if (-not $AzureResourceGroup -or -not $AzureVmName) {
    if (-not $VmHost -or $VmHost -eq "YOUR_VM_PUBLIC_IP") {
        throw "Set VmHost in deploy\azure\laptop.config.ps1, or pass -AzureResourceGroup and -AzureVmName"
    }
    Write-Host "==> Looking up VM with public IP $VmHost..."
    $vmJson = az vm list -d --query "[?publicIps=='$VmHost'] | [0]" -o json | ConvertFrom-Json
    if (-not $vmJson) {
        throw "No VM found with public IP $VmHost."
    }
    $AzureResourceGroup = $vmJson.resourceGroup
    $AzureVmName = $vmJson.name
    Write-Host "    Found: $AzureVmName in $AzureResourceGroup"
}

Write-Host "==> Finding NSG for VM $AzureVmName..."
$nicId = az vm show -g $AzureResourceGroup -n $AzureVmName --query "networkProfile.networkInterfaces[0].id" -o tsv
$nsgId = az network nic show --ids $nicId --query "networkSecurityGroup.id" -o tsv 2>$null
if (-not $nsgId -or $nsgId -eq "null") {
    $subnetId = az network nic show --ids $nicId --query "ipConfigurations[0].subnet.id" -o tsv
    $nsgId = az network vnet subnet show --ids $subnetId --query "networkSecurityGroup.id" -o tsv
}
if (-not $nsgId -or $nsgId -eq "null") {
    throw "Could not find NSG. Add ports 80 and 443 manually in Azure Portal → VM → Networking."
}

$nsgName = Split-Path $nsgId -Leaf
$nsgRg = (az network nsg show --ids $nsgId --query "resourceGroup" -o tsv)

function Ensure-PortRule([int]$Port, [int]$Priority) {
    $ruleName = "Allow-OptionsAdvisor-$Port"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $existing = az network nsg rule show -g $nsgRg --nsg-name $nsgName -n $ruleName 2>$null
    $showOk = ($LASTEXITCODE -eq 0 -and $existing)
    $ErrorActionPreference = $prevEap
    if ($showOk) {
        Write-Host "==> Rule '$ruleName' already exists."
        return
    }
    Write-Host "==> Creating NSG rule TCP $Port (source $SourceIp)..."
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
        --description "Options Advisor HTTPS (Caddy)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create NSG rule $ruleName"
    }
    Write-Host "    Rule created."
}

Ensure-PortRule -Port 80 -Priority $PriorityBase
Ensure-PortRule -Port 443 -Priority ($PriorityBase + 1)

Write-Host ""
Write-Host "Done. After Caddy is up: https://<HTTPS_DOMAIN>"
Write-Host "(Port 80 is required for Let's Encrypt certificate issuance.)"
