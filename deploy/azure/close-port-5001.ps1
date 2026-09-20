# Remove public Azure NSG rule for dashboard HTTP :5001 (HTTPS-only access).
#
# App still listens on 5001 inside Docker; Caddy proxies https:// to that port.
# After this, http://<VmHost>:5001 from the internet will not connect.
#
# Usage (repo root, after az login):
#   .\deploy\azure\close-port-5001.ps1
#
param(
    [string]$VmHost,
    [string]$AzureResourceGroup,
    [string]$AzureVmName,
    [string]$Port = "5001"
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"
if (Test-Path $ConfigFile) { . $ConfigFile }

if (-not $VmHost) { $VmHost = $script:VmHost }
if (-not $AzureResourceGroup) { $AzureResourceGroup = $script:AzureResourceGroup }
if (-not $AzureVmName) { $AzureVmName = $script:AzureVmName }

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI not found. Install: winget install Microsoft.AzureCLI"
}
$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) { throw "Not logged in to Azure. Run: az login" }

if (-not $AzureResourceGroup -or -not $AzureVmName) {
    if (-not $VmHost -or $VmHost -eq "YOUR_VM_PUBLIC_IP") {
        throw "Set VmHost in deploy\azure\laptop.config.ps1"
    }
    Write-Host "==> Looking up VM with public IP $VmHost..."
    $vmJson = az vm list -d --query "[?publicIps=='$VmHost'] | [0]" -o json | ConvertFrom-Json
    if (-not $vmJson) { throw "No VM found with public IP $VmHost" }
    $AzureResourceGroup = $vmJson.resourceGroup
    $AzureVmName = $vmJson.name
}

Write-Host "==> Finding NSG for VM $AzureVmName..."
$nicId = az vm show -g $AzureResourceGroup -n $AzureVmName --query "networkProfile.networkInterfaces[0].id" -o tsv
$nsgId = az network nic show --ids $nicId --query "networkSecurityGroup.id" -o tsv 2>$null
if (-not $nsgId -or $nsgId -eq "null") {
    $subnetId = az network nic show --ids $nicId --query "ipConfigurations[0].subnet.id" -o tsv
    $nsgId = az network vnet subnet show --ids $subnetId --query "networkSecurityGroup.id" -o tsv
}
if (-not $nsgId -or $nsgId -eq "null") {
    throw "Could not find NSG."
}

$nsgName = Split-Path $nsgId -Leaf
$nsgRg = (az network nsg show --ids $nsgId --query "resourceGroup" -o tsv)
$ruleName = "Allow-OptionsAdvisor-$Port"

$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
az network nsg rule show -g $nsgRg --nsg-name $nsgName -n $ruleName 2>$null | Out-Null
$exists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEap

if (-not $exists) {
    Write-Host "==> Rule '$ruleName' already absent - public :$Port is closed."
} else {
    Write-Host "==> Deleting NSG rule '$ruleName'..."
    az network nsg rule delete -g $nsgRg --nsg-name $nsgName -n $ruleName
    if ($LASTEXITCODE -ne 0) { throw "Failed to delete $ruleName" }
    Write-Host "    Deleted."
}

Write-Host ""
Write-Host "Done. Use https://${VmHost}/ only (port 80/443)."
Write-Host "Docker still maps 5001 for Caddy on the internal network."
