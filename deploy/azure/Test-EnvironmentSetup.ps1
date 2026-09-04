# Verify Options Advisor laptop + VM setup against deploy/azure/setup-manifest.json
#
# Usage:
#   .\deploy\azure\Test-EnvironmentSetup.ps1
#   .\deploy\azure\Test-EnvironmentSetup.ps1 -Strict   # fail on non-critical too
#
param(
    [switch]$Strict,
    [switch]$Quiet
)

$ErrorActionPreference = "Continue"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $DeployDir)
$ManifestPath = Join-Path $DeployDir "setup-manifest.json"
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"

if (-not (Test-Path $ManifestPath)) { throw "Missing $ManifestPath" }
$manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json

if (Test-Path $ConfigFile) { . $ConfigFile }

function Write-Check([string]$Status, [string]$Label, [string]$Detail) {
    if ($Quiet -and $Status -eq "PASS") { return }
    $icon = switch ($Status) { "PASS" { "[OK]" } "WARN" { "[!!]" } "FAIL" { "[XX]" } default { "[??]" } }
    $line = "$icon $Label"
    if ($Detail) { $line += " - $Detail" }
    Write-Host $line
}

function Test-CommandExists([string]$CommandLine) {
    $exe = ($CommandLine -split '\s+')[0]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        return @{ ok = $true; detail = (Invoke-Expression $CommandLine 2>&1 | Select-Object -First 1) }
    }
    return @{ ok = $false; detail = "not found" }
}

function Get-ConfigValue([string]$Key) {
    $name = "script:$Key"
    if (Test-Path variable:$name) { return (Get-Variable -Name $Key -Scope Script).Value }
    return $null
}

function Test-SqlServer([string]$Server) {
    if (-not $Server) { return @{ ok = $false; detail = "LocalSqlServer not set" } }
    $q = "sqlcmd -S `"$Server`" -E -Q `"SELECT 1`" -h-1 -W"
    $out = cmd /c $q 2>&1
    if ($LASTEXITCODE -eq 0) { return @{ ok = $true; detail = $Server } }
    return @{ ok = $false; detail = ($out | Out-String).Trim() }
}

function Get-SshArgs() {
    $args = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=accept-new")
    $key = Get-ConfigValue "SshKeyPath"
    if ($key -and (Test-Path $key)) { $args += @("-i", $key) }
    return $args
}

function Get-SshTarget() {
    $vmHost = Get-ConfigValue "VmHost"
    $user = Get-ConfigValue "VmUser"
    if (-not $user) { $user = "azureuser" }
    if (-not $vmHost -or $vmHost -eq "YOUR_VM_PUBLIC_IP") { return $null }
    return "${user}@${vmHost}"
}

$results = @()

foreach ($section in $manifest.sections) {
    if (-not $Quiet) { Write-Host ""; Write-Host "=== $($section.title) ===" }
    foreach ($item in $section.items) {
        $ok = $false
        $detail = ""
        switch ($item.check) {
            "command" {
                $r = Test-CommandExists $item.command
                $ok = $r.ok; $detail = [string]$r.detail
            }
            "file" {
                $p = Join-Path $RepoRoot ($item.path -replace '/', '\')
                $ok = Test-Path $p
                $detail = if ($ok) { $p } else { "missing" }
            }
            "config_path" {
                $p = Get-ConfigValue $item.config_key
                $ok = $p -and (Test-Path $p)
                $detail = if ($ok) { $p } else { "missing or not set" }
            }
            "config_value" {
                $v = Get-ConfigValue $item.config_key
                $ok = $v -and ($v -ne $item.not)
                $detail = if ($v) { $v } else { "not set" }
            }
            "config_dir" {
                $p = Get-ConfigValue $item.config_key
                if ($p) {
                    New-Item -ItemType Directory -Force -Path $p | Out-Null
                    $ok = Test-Path $p
                    $detail = $p
                } else {
                    $ok = $false
                    $detail = "not set in laptop.config.ps1"
                }
            }
            "sql_server" {
                $r = Test-SqlServer (Get-ConfigValue "LocalSqlServer")
                $ok = $r.ok; $detail = [string]$r.detail
            }
            "scheduled_task" {
                $task = schtasks /Query /TN $item.task_name 2>$null
                $ok = ($LASTEXITCODE -eq 0)
                $detail = if ($ok) { "registered" } else { "run register-laptop-archive-task.ps1" }
            }
            "vm_ssh" {
                $target = Get-SshTarget
                if (-not $target) { $ok = $false; $detail = "VmHost not configured" }
                else {
                    $sshArgs = Get-SshArgs
                    & ssh @sshArgs $target "echo OK" 2>$null | Out-Null
                    $ok = ($LASTEXITCODE -eq 0)
                    $detail = if ($ok) { $target } else { "SSH failed" }
                }
            }
            "vm_http" {
                $vmHost = Get-ConfigValue "VmHost"
                if (-not $vmHost -or $vmHost -eq "YOUR_VM_PUBLIC_IP") { $ok = $false; $detail = "VmHost not set" }
                else {
                    $port = if ($item.port) { $item.port } else { 5001 }
                    try {
                        $resp = Invoke-WebRequest -Uri "http://${vmHost}:$port/" -UseBasicParsing -TimeoutSec 10
                        $ok = ($resp.StatusCode -eq 200)
                        $detail = "HTTP $($resp.StatusCode)"
                    } catch {
                        $ok = $false
                        $detail = $_.Exception.Message
                    }
                }
            }
            "vm_compose" {
                $target = Get-SshTarget
                $dir = Get-ConfigValue "VmProjectDir"
                if (-not $dir) { $dir = "/home/azureuser/options_advisor_system" }
                if (-not $target) { $ok = $false; $detail = "VmHost not configured" }
                else {
                    $sshArgs = Get-SshArgs
                    $out = & ssh @sshArgs $target "cd '$dir' && export COMPOSE_PROFILES=bundled && docker compose ps 2>/dev/null" 2>&1 | Out-String
                    $ok = ($LASTEXITCODE -eq 0) -and ($out -match "options_advisor|sqlserver" -and $out -match "Up|running")
                    $detail = if ($ok) { "containers up" } else { "docker compose ps failed or stack down" }
                }
            }
            "azure_uptime" {
                $az = "${env:ProgramFiles}\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
                if (-not (Test-Path $az)) { $ok = $false; $detail = "az not installed" }
                else {
                    $rg = Get-ConfigValue "AzureResourceGroup"
                    $aa = "aa-stockapps-optionsadvisor"
                    if (-not $rg) { $ok = $false; $detail = "AzureResourceGroup not in laptop.config.ps1" }
                    else {
                        $count = & $az automation schedule list --automation-account-name $aa --resource-group $rg --query "length(@)" -o tsv 2>$null
                        $ok = ($LASTEXITCODE -eq 0) -and ([int]$count -ge 2)
                        $detail = if ($ok) { "$count schedules" } else { "run VMUpTimeConfiguration.ps1" }
                    }
                }
            }
            "vm_file" {
                $target = Get-SshTarget
                $dir = Get-ConfigValue "VmProjectDir"
                if (-not $dir) { $dir = "/home/azureuser/options_advisor_system" }
                if (-not $target) { $ok = $false; $detail = "VmHost not configured" }
                else {
                    $remote = "$dir/$($item.remote_path)"
                    $sshArgs = Get-SshArgs
                    & ssh @sshArgs $target "test -f '$remote'" 2>$null
                    $ok = ($LASTEXITCODE -eq 0)
                    $detail = if ($ok) { $remote } else { "missing on VM - git pull + deploy" }
                }
            }
            default {
                $ok = $false
                $detail = "unknown check $($item.check)"
            }
        }

        $status = if ($ok) { "PASS" } elseif ($item.critical) { "FAIL" } else { "WARN" }
        Write-Check $status $item.label $detail
        $results += [PSCustomObject]@{ id = $item.id; critical = [bool]$item.critical; ok = $ok; status = $status }
    }
}

$criticalFail = @($results | Where-Object { $_.critical -and -not $_.ok }).Count
$warnFail = @($results | Where-Object { -not $_.critical -and -not $_.ok }).Count

if (-not $Quiet) {
    Write-Host ""
    Write-Host "Summary: critical failures=$criticalFail warnings=$warnFail"
}

if ($criticalFail -gt 0) { exit 1 }
if ($Strict -and $warnFail -gt 0) { exit 1 }
exit 0
