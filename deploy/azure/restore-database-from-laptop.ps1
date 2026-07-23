# Restore OptionsAdvisorDB ONLY (SQL Server .bak) from laptop → Azure VM.
#
# Does NOT deploy or overwrite application code, logs, data/, or Zerodha session.
# Only runs RESTORE DATABASE on the VM SQL Server container.
#
# Usage (from repo root):
#   .\deploy\azure\restore-database-from-laptop.ps1 -CreateLocalBackup
#   .\deploy\azure\restore-database-from-laptop.ps1 -BackupPath "D:\Backups\OptionsAdvisorDB\file.bak"
#
# Config: copy deploy\azure\laptop.config.ps1.example → deploy\azure\laptop.config.ps1
#
param(
    [string]$BackupPath,
    [switch]$CreateLocalBackup,
    [string]$VmHost,
    [string]$VmUser,
    [string]$VmProjectDir,
    [string]$SshKeyPath,
    [string]$LocalBackupDir,
    [string]$LocalSqlServer,
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
$LocalSqlServer = Resolve-Param $LocalSqlServer $(if ($script:LocalSqlServer) { $script:LocalSqlServer } else { "localhost\SQLEXPRESS" })
$DbName         = Resolve-Param $DbName         $(if ($script:LocalDbName) { $script:LocalDbName } else { "OptionsAdvisorDB" })

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

# --- Step 1: obtain .bak on laptop (database only) ---
if ($CreateLocalBackup) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $BackupPath = Join-Path $LocalBackupDir "${DbName}-${stamp}.bak"
    Write-Host "==> [1/3] SQL BACKUP DATABASE locally (${DbName} only)..."
    Write-Host "    Output: $BackupPath"

    $sql = "BACKUP DATABASE [$DbName] TO DISK = N'$($BackupPath -replace "'", "''")' WITH INIT, COMPRESSION, STATS = 10"
    & sqlcmd -S $LocalSqlServer -E -Q $sql
    if ($LASTEXITCODE -ne 0) {
        throw "sqlcmd backup failed. Install sqlcmd (SSMS) or pass -BackupPath to an existing .bak"
    }
}
elseif (-not $BackupPath -or -not (Test-Path $BackupPath)) {
    throw "Provide -BackupPath to a .bak file, or use -CreateLocalBackup."
}
else {
    Write-Host "==> [1/3] Using existing database backup: $BackupPath"
}

if ($BackupPath -notmatch '\.bak$') {
    throw "Only SQL Server .bak database files are supported."
}

$bakName = Split-Path $BackupPath -Leaf
$remoteBak = "$VmProjectDir/backups/$bakName"

# --- Step 2: upload .bak only ---
Write-Host "==> [2/3] Uploading database file to VM ($sshTarget)..."
& ssh @sshArgs $sshTarget "mkdir -p '$VmProjectDir/backups'"
& scp @scpArgs $BackupPath "${sshTarget}:${remoteBak}"
if ($LASTEXITCODE -ne 0) { throw "scp upload failed" }

# --- Step 3: RESTORE DATABASE only on VM ---
Write-Host "==> [3/3] RESTORE DATABASE on VM (overwrites ${DbName} only)..."
$remoteScript = @"
set -euo pipefail
export COMPOSE_PROFILES=bundled
cd '$VmProjectDir'
# shellcheck disable=SC1091
source .env.docker
DB='${DbName}'
BAK='backups/$bakName'
CONTAINER="/var/opt/mssql/backup/$bakName"
if ! docker compose ps sqlserver 2>/dev/null | grep -q 'Up'; then
  echo "ERROR: sqlserver container is not running." >&2
  exit 1
fi
docker compose stop options_advisor ws_runner 2>/dev/null || true
docker compose exec -T sqlserver mkdir -p /var/opt/mssql/backup
docker cp "\${BAK}" "options_sqlserver:\${CONTAINER}"
docker compose exec -T sqlserver /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P "\${MSSQL_SA_PASSWORD}" -C -b -Q \
  "ALTER DATABASE [\${DB}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
   RESTORE DATABASE [\${DB}] FROM DISK = N'\${CONTAINER}' WITH REPLACE, RECOVERY;
   ALTER DATABASE [\${DB}] SET MULTI_USER;"
docker compose up -d options_advisor ws_runner
echo "DONE_DB_RESTORE \${DB}"
"@
$remoteScript | & ssh @sshArgs $sshTarget "bash -s"

if ($LASTEXITCODE -ne 0) { throw "Database restore failed on VM" }

Write-Host ""
Write-Host "Done. Database restored on VM: $DbName"
Write-Host "(Application code, logs, and data/ on the VM were not changed.)"
Write-Host "Dashboard: http://${VmHost}:5001"
