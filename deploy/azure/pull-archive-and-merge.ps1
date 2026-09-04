# Pull pending archive .bak from VM, merge into local OptionsAdvisorDB_Archive, ACK truncate on VM.
#
# One-time setup:
#   .\deploy\azure\register-laptop-archive-task.ps1
#
# Manual run:
#   .\deploy\azure\pull-archive-and-merge.ps1
#
param(
    [string]$VmHost,
    [string]$VmUser,
    [string]$VmProjectDir,
    [string]$SshKeyPath,
    [string]$LocalArchiveDir,
    [string]$LocalSqlServer,
    [string]$LocalArchiveDb = "OptionsAdvisorDB_Archive",
    [switch]$SkipVmAck
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $DeployDir)
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"
if (Test-Path $ConfigFile) { . $ConfigFile }

function Resolve-Param([string]$Value, [string]$Default) {
    if ($Value) { return $Value }
    if ($Default) { return $Default }
    return $null
}

$VmHost         = Resolve-Param $VmHost $script:VmHost
$VmUser         = Resolve-Param $VmUser $(if ($script:VmUser) { $script:VmUser } else { "azureuser" })
$VmProjectDir   = Resolve-Param $VmProjectDir $script:VmProjectDir
$SshKeyPath     = Resolve-Param $SshKeyPath $script:SshKeyPath
$LocalArchiveDir = Resolve-Param $LocalArchiveDir $(if ($script:LocalArchiveDir) { $script:LocalArchiveDir } else { "D:\Backups\OptionsAdvisorDB\archive" })
$LocalSqlServer = Resolve-Param $LocalSqlServer $(if ($script:LocalSqlServer) { $script:LocalSqlServer } else { "localhost\SQLEXPRESS" })
$LocalArchiveDb = Resolve-Param $LocalArchiveDb $(if ($script:LocalArchiveDb) { $script:LocalArchiveDb } else { "OptionsAdvisorDB_Archive" })

if (-not $VmHost -or $VmHost -eq "YOUR_VM_PUBLIC_IP") {
    throw "Set VmHost in deploy\azure\laptop.config.ps1"
}
if (-not $VmProjectDir) { $VmProjectDir = "/home/$VmUser/options_advisor_system" }

$sshTarget = "${VmUser}@${VmHost}"
$sshArgs = @(); $scpArgs = @()
if ($SshKeyPath) { $sshArgs += @("-i", $SshKeyPath); $scpArgs += @("-i", $SshKeyPath) }

New-Item -ItemType Directory -Force -Path $LocalArchiveDir | Out-Null
$StatePath = Join-Path $LocalArchiveDir "merge-state.json"

function Read-Json([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    return Get-Content $Path -Raw | ConvertFrom-Json
}

function Write-Json([string]$Path, $Obj) {
    $Obj | ConvertTo-Json -Depth 6 | Set-Content -Path $Path -Encoding UTF8
}

Write-Host "==> [1/5] Check VM for pending archive export..."
$remoteManifest = "$VmProjectDir/backups/archive/PENDING.json"
$localManifest = Join-Path $LocalArchiveDir "PENDING.json"

& scp @scpArgs "${sshTarget}:${remoteManifest}" $localManifest 2>$null
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $localManifest)) {
    Write-Host "No pending archive on VM (nothing to do)."
    exit 0
}

$manifest = Read-Json $localManifest
$bakName = $manifest.bak_name
if (-not $bakName) { throw "Invalid PENDING.json: missing bak_name" }

$state = Read-Json $StatePath
if ($state -and $state.last_merged_bak -eq $bakName) {
    Write-Host "Already merged $bakName - skipping."
    exit 0
}

$localBak = Join-Path $LocalArchiveDir $bakName
Write-Host "==> [2/5] Download $bakName ..."
& scp @scpArgs "${sshTarget}:$VmProjectDir/backups/archive/$bakName" $localBak
if ($LASTEXITCODE -ne 0) { throw "scp download failed" }
if ((Get-Item $localBak).Length -lt 1024) { throw "Downloaded .bak too small - aborting" }

Write-Host "==> [3/5] Merge into ${LocalArchiveDb} on ${LocalSqlServer} ..."
$mergeScript = Join-Path $RepoRoot "scripts\merge_archive_into_local.py"
if (-not (Test-Path $mergeScript)) { throw "Missing $mergeScript" }

& python $mergeScript --bak $localBak --server $LocalSqlServer --target-db $LocalArchiveDb
if ($LASTEXITCODE -ne 0) { throw "merge_archive_into_local.py failed" }

Write-Host "==> [4/5] Record merge state"
Write-Json $StatePath @{
    last_merged_bak = $bakName
    merged_at       = (Get-Date).ToString("o")
    target_db       = $LocalArchiveDb
}

if (-not $SkipVmAck) {
    Write-Host "==> [5/5] ACK on VM (truncate *_Archive) ..."
    & ssh @sshArgs $sshTarget "cd '$VmProjectDir' && chmod +x deploy/archive-truncate-vm.sh && ./deploy/archive-truncate-vm.sh"
    if ($LASTEXITCODE -ne 0) { throw "VM truncate/ack failed - local merge OK but VM still has pending rows" }
} else {
    Write-Host "==> [5/5] Skipped VM ACK (-SkipVmAck)"
}

Write-Host ""
Write-Host "Done. Cumulative archive DB: ${LocalArchiveDb} on ${LocalSqlServer}"
Write-Host "Chunk saved: $localBak"
