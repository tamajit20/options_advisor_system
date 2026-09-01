# Backup OptionsAdvisorDB ONLY (SQL Server .bak) from Azure VM → Windows laptop.
#
# Does NOT copy application code, logs, data/, or Zerodha session files.
#
# Usage (from repo root):
#   .\deploy\azure\backup-database-to-laptop.ps1
#
# Config: copy deploy\azure\laptop.config.ps1.example → deploy\azure\laptop.config.ps1
#
param(
    [string]$VmHost,
    [string]$VmUser,
    [string]$VmProjectDir,
    [string]$SshKeyPath,
    [string]$LocalBackupDir,
    [string]$DbName = "OptionsAdvisorDB"
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"

if (Test-Path $ConfigFile) { . $ConfigFile }

function Resolve-Param([string]$Value, [string]$Default) {
    if ($Value) { return $Value }
    if ($Default) { return $Default }
    return $null
}

$VmHost       = Resolve-Param $VmHost       $script:VmHost
$VmUser       = Resolve-Param $VmUser       $(if ($script:VmUser) { $script:VmUser } else { "azureuser" })
$VmProjectDir = Resolve-Param $VmProjectDir $script:VmProjectDir
$SshKeyPath   = Resolve-Param $SshKeyPath   $script:SshKeyPath
$LocalBackupDir = Resolve-Param $LocalBackupDir $(if ($script:LocalBackupDir) { $script:LocalBackupDir } else { "D:\Backups\OptionsAdvisorDB" })
$DbName       = Resolve-Param $DbName       $(if ($script:LocalDbName) { $script:LocalDbName } else { "OptionsAdvisorDB" })

if (-not $VmHost -or $VmHost -eq "YOUR_VM_PUBLIC_IP") {
    throw "Set VmHost in deploy\azure\laptop.config.ps1 or pass -VmHost"
}
if (-not $VmProjectDir) {
    $VmProjectDir = "/home/$VmUser/options_advisor_system"
}

$sshTarget = "${VmUser}@${VmHost}"
$sshArgs = @()
$scpArgs = @()
if ($SshKeyPath) {
    $sshArgs += @("-i", $SshKeyPath)
    $scpArgs += @("-i", $SshKeyPath)
}

New-Item -ItemType Directory -Force -Path $LocalBackupDir | Out-Null

Write-Host "==> [1/2] SQL BACKUP DATABASE on VM (${DbName} only)..."
$remoteScript = @'
set -euo pipefail
export COMPOSE_PROFILES=bundled
cd '__VM_PROJECT_DIR__'
# shellcheck disable=SC1091
set -a
source .env.docker
set +a
DB='__DB_NAME__'
STAMP=$(date +%Y%m%d-%H%M%S)
REL="backups/${DB}-${STAMP}.bak"
CONTAINER="/var/opt/mssql/backup/${DB}-${STAMP}.bak"
mkdir -p backups
if ! docker compose ps sqlserver 2>/dev/null | grep -qE 'Up|healthy'; then
  echo "ERROR: sqlserver container is not running." >&2
  exit 1
fi
docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b -Q \
  "BACKUP DATABASE [${DB}] TO DISK = N'${CONTAINER}' WITH INIT, STATS = 10" >/dev/null
docker cp "options_sqlserver:${CONTAINER}" "${REL}"
echo "DONE_DB_BACKUP ${REL}"
'@.Replace('__VM_PROJECT_DIR__', $VmProjectDir).Replace('__DB_NAME__', $DbName)
$remoteScript = $remoteScript -replace "`r`n", "`n" -replace "`r", "`n"
$backupOutput = $remoteScript | & ssh @sshArgs $sshTarget "bash -s" 2>&1
$backupExit = $LASTEXITCODE
$backupOutput | ForEach-Object { Write-Host $_ }

$remoteRel = $null
foreach ($line in ($backupOutput -split "`n")) {
    if ($line -match 'DONE_DB_BACKUP\s+(backups/\S+\.bak)') {
        $remoteRel = $Matches[1]
        break
    }
}
if (-not $remoteRel) {
    if ($backupExit -ne 0) {
        throw "Database backup failed on VM (ssh exit $backupExit). Check sqlserver is running."
    }
    throw "Database backup failed on VM. DONE_DB_BACKUP marker not found in output."
}

$remoteFull = "$VmProjectDir/$($remoteRel -replace '\\','/')"
$bakName = Split-Path $remoteRel -Leaf
if ($bakName -notmatch '\.bak$') {
    throw "Expected a .bak database file, got: $bakName"
}
$localPath = Join-Path $LocalBackupDir $bakName

Write-Host "==> [2/2] Downloading database file only: $localPath"
& scp @scpArgs "${sshTarget}:${remoteFull}" $localPath
if ($LASTEXITCODE -ne 0) { throw "scp download failed" }

$sizeMb = [math]::Round((Get-Item $localPath).Length / 1MB, 2)
Write-Host ""
Write-Host "Done. Database backup only: $localPath ($sizeMb MB)"
Write-Host "(Application code, logs, and data/ were not copied.)"
