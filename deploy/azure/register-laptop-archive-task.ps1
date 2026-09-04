# Register Windows Task Scheduler job: pull VM archive + merge locally (Mon-Fri 09:15).
#
# Run once from repo root (elevated not required):
#   .\deploy\azure\register-laptop-archive-task.ps1
#
param(
    [string]$TaskName = "OptionsAdvisor-ArchiveMerge",
    [string]$Time = "09:15"
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $DeployDir)
$ScriptPath = Join-Path $DeployDir "pull-archive-and-merge.ps1"

if (-not (Test-Path $ScriptPath)) { throw "Missing $ScriptPath" }

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument `
    "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" -WorkingDirectory $RepoRoot

# Mon-Fri after VM starts (08:55). Catches Fri export on Monday if laptop was off.
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Pull Options Advisor archive .bak from Azure VM, merge into local OptionsAdvisorDB_Archive, ACK VM truncate." `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "  Runs: Mon-Fri at $Time (pull + merge when VM has pending export)"
Write-Host "  Script: $ScriptPath"
Write-Host ""
Write-Host 'Test now: .\deploy\azure\pull-archive-and-merge.ps1'
