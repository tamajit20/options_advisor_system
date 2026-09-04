# One-time laptop setup: config, folders, archive scheduled task, verification.
#
# Called by setup-new-environment.ps1. See readmefirst.txt at repo root.
#   copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
#   notepad deploy\azure\laptop.config.ps1
#   .\deploy\azure\setup-laptop.ps1
#
param(
    [switch]$SkipArchiveTask,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $DeployDir)
$ExampleConfig = Join-Path $DeployDir "laptop.config.ps1.example"
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"

Write-Host "==> Options Advisor — laptop setup"
Write-Host ""

if (-not (Test-Path $ConfigFile)) {
    if (-not (Test-Path $ExampleConfig)) { throw "Missing $ExampleConfig" }
    Copy-Item $ExampleConfig $ConfigFile
    Write-Host "Created $ConfigFile from example."
    Write-Host "EDIT REQUIRED: VmHost, SshKeyPath, LocalSqlServer, AzureResourceGroup"
    Write-Host "Then re-run: .\deploy\azure\setup-laptop.ps1"
    exit 2
}

. $ConfigFile

$backupDir = if ($script:LocalBackupDir) { $script:LocalBackupDir } else { "D:\Backups\OptionsAdvisorDB" }
$archiveDir = if ($script:LocalArchiveDir) { $script:LocalArchiveDir } else { Join-Path $backupDir "archive" }
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
New-Item -ItemType Directory -Force -Path $archiveDir | Out-Null
Write-Host "Backup dir:  $backupDir"
Write-Host "Archive dir: $archiveDir"

if (-not $SkipArchiveTask) {
    Write-Host ""
    Write-Host "==> Registering archive merge scheduled task..."
    & (Join-Path $DeployDir "register-laptop-archive-task.ps1")
}

if (-not $SkipVerify) {
    Write-Host ""
    Write-Host "==> Verifying laptop setup..."
    & (Join-Path $DeployDir "Test-EnvironmentSetup.ps1")
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Some checks failed — fix laptop.config.ps1 or install missing tools, then re-run."
        exit 1
    }
}

Write-Host ""
Write-Host "Laptop setup complete."
