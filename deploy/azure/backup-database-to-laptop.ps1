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

if ($DbName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    throw "Unsafe database name: $DbName"
}

Write-Host "==> [1/3] SQL BACKUP DATABASE on VM (${DbName} only)..."
$remoteScript = @'
set -euo pipefail
export COMPOSE_PROFILES=bundled
cd '__VM_PROJECT_DIR__'
# shellcheck disable=SC1091
set -a
source .env.docker
set +a
DB='__DB_NAME__'
REL="backups/${DB}-latest.bak"
BIND_DIR="/var/opt/mssql/host-backups"
INTERNAL_DIR="/var/opt/mssql/backup"
mkdir -p backups
chmod 777 backups 2>/dev/null || true
if ! docker compose ps sqlserver 2>/dev/null | grep -qE 'Up|healthy'; then
  echo "ERROR: sqlserver container is not running." >&2
  exit 1
fi
if docker compose exec -T sqlserver bash -c "test -w '${BIND_DIR}'"; then
  CONTAINER_PATH="${BIND_DIR}/${DB}-latest.bak"
  docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b -Q \
    "BACKUP DATABASE [${DB}] TO DISK = N'${CONTAINER_PATH}' WITH INIT, STATS = 10"
  if [[ ! -f "${REL}" ]]; then
    echo "ERROR: backup finished but ${REL} is missing (bind-mount ./backups)." >&2
    exit 1
  fi
else
  CONTAINER_PATH="${INTERNAL_DIR}/${DB}-latest.bak"
  docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U sa -P "${MSSQL_SA_PASSWORD}" -C -b -Q \
    "BACKUP DATABASE [${DB}] TO DISK = N'${CONTAINER_PATH}' WITH INIT, STATS = 10"
  docker cp "options_sqlserver:${CONTAINER_PATH}" "${REL}"
  docker compose exec -T sqlserver rm -f "${CONTAINER_PATH}" || true
fi
find backups -maxdepth 1 -type f -name "${DB}-*.bak" ! -name "${DB}-latest.bak" -delete
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
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$localPath = Join-Path $LocalBackupDir "$DbName-$stamp.bak"

Write-Host "==> [2/3] Downloading database file only: $localPath"
& scp @scpArgs "${sshTarget}:${remoteFull}" $localPath
if ($LASTEXITCODE -ne 0) { throw "scp download failed" }
if ((Get-Item $localPath).Length -lt 1024) { throw "Downloaded .bak too small - aborting (VM file kept)" }

Write-Host "==> [3/3] Removing hot backup copy from VM..."
$remoteClean = @"
set -euo pipefail
export COMPOSE_PROFILES=bundled
cd '$VmProjectDir'
rm -f 'backups/$DbName-latest.bak'
find backups -maxdepth 1 -type f -name '$DbName-*.bak' -delete
docker compose exec -T sqlserver rm -f '/var/opt/mssql/backup/$DbName-latest.bak' 2>/dev/null || true
echo DONE_VM_HOT_BACKUP_REMOVED
"@
$remoteClean = $remoteClean -replace "`r`n", "`n" -replace "`r", "`n"
$cleanOut = $remoteClean | & ssh @sshArgs $sshTarget "bash -s" 2>&1
$cleanOut | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Laptop copy OK at $localPath but VM hot .bak may still be present."
}

$sizeMb = [math]::Round((Get-Item $localPath).Length / 1MB, 2)
Write-Host ""
Write-Host "Done. Database backup only: $localPath ($sizeMb MB)"
Write-Host "VM copy removed. (Application code, logs, and data/ were not copied.)"

