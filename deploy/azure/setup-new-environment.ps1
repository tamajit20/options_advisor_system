# Greenfield setup: new laptop + new Azure VM (config, VM install, uptime, laptop automation).
#
# Entry point for fresh install. See README.md Part A at repo root.
#
# Usage:
#   copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
#   copy .env.docker.example .env.docker
#   notepad deploy\azure\laptop.config.ps1
#   notepad .env.docker
#   az login
#   .\deploy\azure\setup-new-environment.ps1
#
# Optional flags:
#   -SkipVmInstall       laptop only (VM already installed)
#   -SkipVmUptime        skip Azure Automation schedules
#   -SkipArchiveTask     skip Windows scheduled merge task
#   -RestoreFromBackup   path to .bak to push to new VM after install
#   -FreshDb             empty DB on VM (no restore)
#
param(
    [string]$EnvFile,
    [string]$RestoreFromBackup,
    [switch]$SkipVmInstall,
    [switch]$SkipVmUptime,
    [switch]$SkipArchiveTask,
    [switch]$FreshDb,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $DeployDir)
$LaptopConfig = Join-Path $DeployDir "laptop.config.ps1"
if (Test-Path $LaptopConfig) { . $LaptopConfig }

function Ensure-OsDiskStandardSsd {
    param([string]$AzCmd)
    $rg = if ($script:AzureResourceGroup) { $script:AzureResourceGroup } else { "STOCKAPPS" }
    $vm = if ($script:AzureVmName) { $script:AzureVmName } else { "OptionsAdvisor" }
    $targetSku = "StandardSSD_LRS"
    Write-Host "==> Ensure OS disk is Standard SSD (not Premium) for VM '$vm'"
    $diskName = & $AzCmd vm show -g $rg -n $vm --query "storageProfile.osDisk.name" -o tsv 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $diskName) {
        Write-Host "WARNING: could not read OS disk — skip SKU ensure"
        return
    }
    $currentSku = & $AzCmd disk show -g $rg -n $diskName --query "sku.name" -o tsv 2>$null
    if ($currentSku -eq $targetSku) {
        Write-Host "    OS disk already $targetSku"
        return
    }
    Write-Host "    Converting $diskName from $currentSku -> $targetSku (VM must be deallocated)"
    $power = & $AzCmd vm get-instance-view -g $rg -n $vm `
        --query "instanceView.statuses[?starts_with(code, 'PowerState/')].displayStatus | [0]" -o tsv 2>$null
    $wasRunning = ($power -match "running")
    if ($wasRunning) {
        & $AzCmd vm deallocate -g $rg -n $vm
        if ($LASTEXITCODE -ne 0) { throw "az vm deallocate failed during OS disk SKU ensure" }
    }
    & $AzCmd disk update -g $rg -n $diskName --sku $targetSku
    if ($LASTEXITCODE -ne 0) { throw "az disk update to $targetSku failed" }
    if ($wasRunning) {
        & $AzCmd vm start -g $rg -n $vm
        if ($LASTEXITCODE -ne 0) { throw "az vm start failed after OS disk SKU ensure" }
    }
    Write-Host "    OS disk is now $targetSku"
}

Write-Host "============================================================"
Write-Host " Options Advisor — new environment setup"
Write-Host "============================================================"
Write-Host ""

# --- Step 1: Laptop ---
Write-Host "==> [1/5] Laptop setup"
$laptopArgs = @()
if ($SkipArchiveTask) { $laptopArgs += "-SkipArchiveTask" }
if ($SkipVerify) { $laptopArgs += "-SkipVerify" }
& (Join-Path $DeployDir "setup-laptop.ps1") @laptopArgs
if ($LASTEXITCODE -eq 2) { exit 2 }
if ($LASTEXITCODE -ne 0) { throw "setup-laptop.ps1 failed" }

# --- Step 2: VM install ---
if (-not $SkipVmInstall) {
    Write-Host ""
    Write-Host "==> [2/5] VM install (Docker + app + self-signed HTTPS)"
    $vmArgs = @{}
    if ($EnvFile) { $vmArgs.EnvFile = $EnvFile }
    elseif (Test-Path (Join-Path $RepoRoot ".env.docker")) { $vmArgs.EnvFile = (Join-Path $RepoRoot ".env.docker") }
    if ($FreshDb) { $vmArgs.FreshDb = $true }
    & (Join-Path $DeployDir "remote-vm-install.ps1") @vmArgs
    if ($LASTEXITCODE -ne 0) { throw "remote-vm-install.ps1 failed" }
} else {
    Write-Host ""
    Write-Host "==> [2/5] Skipped VM install (-SkipVmInstall)"
}

# --- Step 3: VM uptime schedules + Standard SSD OS disk ---
if (-not $SkipVmUptime) {
    Write-Host ""
    Write-Host "==> [3/5] VM uptime (Mon-Fri 08:55-15:45 IST) + OS disk Standard SSD"
    $az = "${env:ProgramFiles}\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
    if (-not (Test-Path $az)) {
        Write-Host "WARNING: Azure CLI not found — skip VM uptime / disk SKU or install: winget install Microsoft.AzureCLI"
    } else {
        & (Join-Path $DeployDir "VMUpTimeConfiguration.ps1")
        Ensure-OsDiskStandardSsd -AzCmd $az
    }
} else {
    Write-Host ""
    Write-Host "==> [3/5] Skipped VM uptime (-SkipVmUptime); OS disk Standard SSD ensure also skipped"
}

# --- Step 4: Optional DB restore ---
if ($RestoreFromBackup) {
    Write-Host ""
    Write-Host "==> [4/5] Restore database to VM from $RestoreFromBackup"
    & (Join-Path $DeployDir "restore-database-from-laptop.ps1") -BackupPath $RestoreFromBackup
    if ($LASTEXITCODE -ne 0) { throw "restore-database-from-laptop.ps1 failed" }
} else {
    Write-Host ""
    Write-Host "==> [4/5] Database restore skipped (new empty DB unless you used -RestoreFromBackup)"
}

# --- Step 5: Verify ---
if (-not $SkipVerify) {
    Write-Host ""
    Write-Host "==> [5/5] Full environment verification"
    & (Join-Path $DeployDir "Test-EnvironmentSetup.ps1")
    $verifyExit = $LASTEXITCODE
    $syncScript = Join-Path $RepoRoot "scripts\validate_setup_sync.py"
    if (Test-Path $syncScript) {
        Write-Host ""
        Write-Host "==> Setup sync validation"
        & python $syncScript
        if ($LASTEXITCODE -ne 0) { $verifyExit = 1 }
    }
} else {
    Write-Host ""
    Write-Host "==> [5/5] Verification skipped (-SkipVerify)"
    $verifyExit = 0
}

Write-Host ""
Write-Host "============================================================"
Write-Host " Setup finished."
Write-Host ""
Write-Host " Next (manual, each trading morning on VM):"
Write-Host "   Zerodha login via dashboard or: python main.py --zerodha-login"
Write-Host ""
Write-Host " Dashboard: https://$($script:VmHost)/  (self-signed — accept browser warning once)"
Write-Host "            (public HTTP :5001 closed — HTTPS only)"
Write-Host ""
Write-Host " Automated weekly:"
Write-Host "   VM Fri: archive + export | Laptop Mon-Fri 09:15: pull until ACK, then wait"
Write-Host ""
Write-Host " Re-check anytime: .\deploy\azure\Test-EnvironmentSetup.ps1"
Write-Host "============================================================"

if ($verifyExit -ne 0) { exit 1 }
exit 0
