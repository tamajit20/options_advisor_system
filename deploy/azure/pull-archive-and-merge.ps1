# Pull VM backups onto the laptop, merge the archive chunk, then ACK.
# VM hot rows / export files are deleted only after this script has local copies
# and merge succeeded.
#
# Scheduled once Mon-Fri at 09:15 (StartWhenAvailable if the laptop was off).
# This process retries until pull/merge/ACK succeeds or the VM window ends
# (~15:45), then exits. Task Scheduler does not fire again until the next
# weekday 09:15. After ACK, later scheduled runs no-op until the next Friday
# PENDING.json.
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
    [string]$LocalBackupDir,
    [string]$LocalSqlServer,
    [string]$LocalArchiveDb = "OptionsAdvisorDB_Archive",
    [string]$DbName = "OptionsAdvisorDB",
    [switch]$SkipVmAck,
    [switch]$Once
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
$LocalBackupDir = Resolve-Param $LocalBackupDir $(if ($script:LocalBackupDir) { $script:LocalBackupDir } else { "D:\Backups\OptionsAdvisorDB" })
$LocalSqlServer = Resolve-Param $LocalSqlServer $(if ($script:LocalSqlServer) { $script:LocalSqlServer } else { "localhost\SQLEXPRESS" })
$LocalArchiveDb = Resolve-Param $LocalArchiveDb $(if ($script:LocalArchiveDb) { $script:LocalArchiveDb } else { "OptionsAdvisorDB_Archive" })
$DbName         = Resolve-Param $DbName $(if ($script:LocalDbName) { $script:LocalDbName } else { "OptionsAdvisorDB" })

if (-not $VmHost -or $VmHost -eq "YOUR_VM_PUBLIC_IP") {
    throw "Set VmHost in deploy\azure\laptop.config.ps1"
}
if (-not $VmProjectDir) { $VmProjectDir = "/home/$VmUser/options_advisor_system" }

$sshTarget = "${VmUser}@${VmHost}"
$sshArgs = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
$scpArgs = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
if ($SshKeyPath) { $sshArgs += @("-i", $SshKeyPath); $scpArgs += @("-i", $SshKeyPath) }

New-Item -ItemType Directory -Force -Path $LocalArchiveDir | Out-Null
New-Item -ItemType Directory -Force -Path $LocalBackupDir | Out-Null
$StatePath = Join-Path $LocalArchiveDir "merge-state.json"

function Read-Json([string]$Path) {
    if (-not (Test-Path $Path)) { return $null }
    return Get-Content $Path -Raw | ConvertFrom-Json
}

function Write-Json([string]$Path, $Obj) {
    $Obj | ConvertTo-Json -Depth 6 | Set-Content -Path $Path -Encoding UTF8
}

function Copy-FromVm([string]$RemoteRel, [string]$LocalPath) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & scp @scpArgs "${sshTarget}:${VmProjectDir}/${RemoteRel}" $LocalPath 2>$null | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    return $code
}

function Test-VmReachable {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & ssh @sshArgs $sshTarget "echo OK" 2>$null | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    return $code -eq 0
}

function Save-HotBackupFromVm([switch]$Required) {
    $markerPath = Join-Path $LocalArchiveDir "LAST_HOT_BACKUP.json"
    $markerCode = Copy-FromVm "backups/archive/LAST_HOT_BACKUP.json" $markerPath
    $marker = $null
    if ($markerCode -eq 0 -and (Test-Path $markerPath) -and ((Get-Item $markerPath).Length -ge 20)) {
        $marker = Read-Json $markerPath
    }
    $state = Read-Json $StatePath
    if ($marker -and $state -and $state.last_hot_backup_at -and ($state.last_hot_backup_at -eq $marker.completed_at)) {
        Write-Host "Hot backup already on laptop for $($marker.completed_at); skip download."
        if ($state.last_hot_bak) { return [string]$state.last_hot_bak }
    }

    $hotName = "${DbName}-latest.bak"
    $localLatest = Join-Path $LocalBackupDir $hotName
    $code = Copy-FromVm "backups/$hotName" $localLatest
    $ok = ($code -eq 0) -and (Test-Path $localLatest) -and ((Get-Item $localLatest).Length -ge 1024)
    if (-not $ok) {
        Remove-Item $localLatest -ErrorAction SilentlyContinue
        if ($Required) {
            throw "Hot backup backups/$hotName is not on the laptop yet. VM data will not be deleted; will retry."
        }
        return $null
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stamped = Join-Path $LocalBackupDir "${DbName}-$stamp.bak"
    Copy-Item $localLatest $stamped -Force
    Write-Host "Hot backup saved: $stamped"
    $st = Read-Json $StatePath
    if (-not $st) { $st = [pscustomobject]@{} }
    $props = @{
        last_merged_bak     = $(if ($st.last_merged_bak) { $st.last_merged_bak } else { $null })
        last_acked_bak      = $(if ($st.last_acked_bak) { $st.last_acked_bak } else { $null })
        last_hot_bak        = $stamped
        last_hot_backup_at  = $(if ($marker) { $marker.completed_at } else { $null })
        target_db           = $LocalArchiveDb
        merged_at           = $(if ($st.merged_at) { $st.merged_at } else { $null })
    }
    Write-Json $StatePath $props
    return $stamped
}

function Invoke-PullAndMergeWork {
    if (-not (Test-VmReachable)) {
        throw "VM not reachable (off or network)."
    }

    Write-Host "==> [1/6] Check VM for pending archive export..."
    $remoteManifest = "$VmProjectDir/backups/archive/PENDING.json"
    $localManifest = Join-Path $LocalArchiveDir "PENDING.json"

    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & scp @scpArgs "${sshTarget}:${remoteManifest}" $localManifest 2>$null | Out-Null
    $scpCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $manifestOk = (Test-Path $localManifest) -and ((Get-Item $localManifest).Length -ge 20)
    if ($scpCode -ne 0 -or -not $manifestOk) {
        Remove-Item $localManifest -ErrorAction SilentlyContinue
        Write-Host "No pending archive export. Pulling hot .bak if a new one exists."
        $pulled = Save-HotBackupFromVm
        if ($pulled) {
            Write-Host "Done. Hot backup on laptop: $pulled"
        } else {
            Write-Host "Nothing pending. Waiting for next scheduled run."
        }
        return
    }

    $manifest = Read-Json $localManifest
    $bakName = $manifest.bak_name
    if (-not $bakName) { throw "Invalid PENDING.json: missing bak_name" }

    $state = Read-Json $StatePath
    $alreadyMerged = $state -and $state.last_merged_bak -eq $bakName
    $alreadyAcked = $state -and $state.last_acked_bak -eq $bakName
    if ($alreadyAcked) {
        Write-Host "Already merged and ACK'd $bakName - waiting for next Friday export."
        return
    }

    Write-Host "==> [2/6] Download hot backup to laptop (required before VM delete)..."
    $hotLocal = Save-HotBackupFromVm -Required

    $localBak = Join-Path $LocalArchiveDir $bakName
    if (-not $alreadyMerged) {
        Write-Host "==> [3/6] Download archive chunk $bakName ..."
        & scp @scpArgs "${sshTarget}:$VmProjectDir/backups/archive/$bakName" $localBak
        if ($LASTEXITCODE -ne 0) { throw "scp download of archive chunk failed; VM files kept; will retry" }
        if ((Get-Item $localBak).Length -lt 1024) { throw "Downloaded archive .bak too small - aborting; VM files kept" }

        Write-Host "==> [4/6] Merge into ${LocalArchiveDb} on ${LocalSqlServer} ..."
        $mergeScript = Join-Path $RepoRoot "scripts\merge_archive_into_local.py"
        if (-not (Test-Path $mergeScript)) { throw "Missing $mergeScript" }

        & python $mergeScript --bak $localBak --server $LocalSqlServer --target-db $LocalArchiveDb
        if ($LASTEXITCODE -ne 0) { throw "merge_archive_into_local.py failed; VM files kept; will retry" }
    } else {
        if (-not (Test-Path $localBak) -or ((Get-Item $localBak).Length -lt 1024)) {
            throw "Previous merge recorded for $bakName but local chunk is missing; VM files kept; will retry"
        }
        Write-Host "==> [3/6] Already merged $bakName locally"
        Write-Host "==> [4/6] Retrying ACK only"
    }

    Write-Host "==> [5/6] Record laptop copies"
    $hotAt = $null
    $stNow = Read-Json $StatePath
    if ($stNow) { $hotAt = $stNow.last_hot_backup_at }
    Write-Json $StatePath @{
        last_merged_bak    = $bakName
        last_acked_bak     = $(if ($state) { $state.last_acked_bak } else { $null })
        last_hot_bak       = $hotLocal
        last_hot_backup_at = $hotAt
        merged_at          = (Get-Date).ToString("o")
        target_db          = $LocalArchiveDb
    }

    if (-not $SkipVmAck) {
        Write-Host "==> [6/6] ACK on VM (delete hot rows only after laptop copies exist) ..."
        & ssh @sshArgs $sshTarget "cd '$VmProjectDir' && chmod +x deploy/archive-truncate-vm.sh && ./deploy/archive-truncate-vm.sh"
        if ($LASTEXITCODE -ne 0) { throw "VM ACK failed - laptop copies OK; will retry ACK" }
        $stNow = Read-Json $StatePath
        Write-Json $StatePath @{
            last_merged_bak    = $bakName
            last_acked_bak     = $bakName
            last_hot_bak       = $hotLocal
            last_hot_backup_at = $(if ($stNow) { $stNow.last_hot_backup_at } else { $null })
            merged_at          = (Get-Date).ToString("o")
            acked_at           = (Get-Date).ToString("o")
            target_db          = $LocalArchiveDb
        }
    } else {
        Write-Host "==> [6/6] Skipped VM ACK (-SkipVmAck)"
    }

    Write-Host ""
    Write-Host "Done. Cumulative archive DB: ${LocalArchiveDb} on ${LocalSqlServer}"
    Write-Host "Archive chunk: $localBak"
    Write-Host "Hot backup: $hotLocal"
}

$RetryMinutes = 15
$WindowEnd = Get-Date -Hour 15 -Minute 45 -Second 0
while ($true) {
    try {
        Invoke-PullAndMergeWork
        Write-Host "Work complete. Next run is the next scheduled Mon-Fri 09:15."
        exit 0
    } catch {
        Write-Host $_
        if ($Once) { exit 1 }
        $now = Get-Date
        if ($now -ge $WindowEnd) {
            Write-Host "VM window ended (~15:45). Stopping until next scheduled 09:15."
            exit 1
        }
        $wait = [Math]::Min($RetryMinutes * 60, [Math]::Max(1, [int]($WindowEnd - $now).TotalSeconds))
        Write-Host "Retrying in $wait seconds (stops after success or 15:45)."
        Start-Sleep -Seconds $wait
    }
}
