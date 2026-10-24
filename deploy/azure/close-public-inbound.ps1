# Delete inbound NSG allows for SSH and the dashboard so the public address
# no longer answers. Tailscale reaches sshd and Caddy on the VM itself.
#
# Usage (repo root, after az login, and after Tailscale SSH works):
#   .\deploy\azure\close-public-inbound.ps1
#
param(
    [string]$AzureResourceGroup,
    [string]$AzureVmName
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"
if (Test-Path $ConfigFile) { . $ConfigFile }

if (-not $AzureResourceGroup) { $AzureResourceGroup = $script:AzureResourceGroup }
if (-not $AzureVmName) { $AzureVmName = $script:AzureVmName }
if (-not $AzureResourceGroup) { $AzureResourceGroup = "STOCKAPPS" }
if (-not $AzureVmName) { $AzureVmName = "OptionsAdvisor" }

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI not found. Install: winget install Microsoft.AzureCLI"
}
$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) { throw "Not logged in to Azure. Run: az login" }

Write-Host "==> Finding NSG for VM $AzureVmName..."
$nicId = az vm show -g $AzureResourceGroup -n $AzureVmName --query "networkProfile.networkInterfaces[0].id" -o tsv
if ($LASTEXITCODE -ne 0 -or -not $nicId) { throw "Could not read VM NIC." }
$nsgId = az network nic show --ids $nicId --query "networkSecurityGroup.id" -o tsv 2>$null
if (-not $nsgId -or $nsgId -eq "null") {
    $subnetId = az network nic show --ids $nicId --query "ipConfigurations[0].subnet.id" -o tsv
    $nsgId = az network vnet subnet show --ids $subnetId --query "networkSecurityGroup.id" -o tsv
}
if (-not $nsgId -or $nsgId -eq "null") { throw "Could not find NSG." }

$nsgName = Split-Path $nsgId -Leaf
$nsgRg = (az network nsg show --ids $nsgId --query "resourceGroup" -o tsv)
$rules = az network nsg rule list -g $nsgRg --nsg-name $nsgName -o json | ConvertFrom-Json
$closePorts = @("22", "80", "443", "5001")
$knownNames = @(
    "SSH",
    "InboundSecurityRulePort5001Open",
    "Allow-OptionsAdvisor-22",
    "Allow-OptionsAdvisor-80",
    "Allow-OptionsAdvisor-443",
    "Allow-OptionsAdvisor-5001"
)

foreach ($rule in @($rules)) {
    if ($rule.direction -ne "Inbound" -or $rule.access -ne "Allow") { continue }
    $ports = @()
    if ($rule.destinationPortRange) { $ports += [string]$rule.destinationPortRange }
    if ($rule.destinationPortRanges) { $ports += @($rule.destinationPortRanges) }
    $byName = $knownNames -contains $rule.name
    $byPort = $false
    foreach ($p in $ports) {
        if ($closePorts -contains $p) { $byPort = $true }
    }
    if (-not $byName -and -not $byPort) { continue }
    Write-Host "==> Deleting inbound rule $($rule.name) (port $($ports -join ','))..."
    az network nsg rule delete -g $nsgRg --nsg-name $nsgName -n $rule.name
    if ($LASTEXITCODE -ne 0) { throw "Failed to delete NSG rule $($rule.name)" }
}

Write-Host "Public inbound 22, 80, 443, and 5001 are closed."
Write-Host "The public IP stays for outbound calls. Reach the VM through Tailscale."
