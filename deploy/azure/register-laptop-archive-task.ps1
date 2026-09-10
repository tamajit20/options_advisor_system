# Register Windows Task Scheduler job: pull VM archive + merge locally.
#
# Fires once Mon-Fri at 09:15. The script itself retries until ACK succeeds
# or the VM window ends (~15:45), then stops until the next scheduled 09:15.
# If the laptop was off, StartWhenAvailable runs it when you next log in.
#
# Run once from repo root:
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

# Mon-Fri after VM starts (08:55). Script may stay alive until ~15:45 only if
# it is still retrying unfinished work.
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $Time

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 7)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Once daily Mon-Fri 09:15: pull hot+archive .bak, retry until ACK, then wait for next 09:15." `
    -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "  Runs: once Mon-Fri at $Time (not every 15 min)"
Write-Host "  If laptop/VM is off, this run retries until ACK or ~15:45, then waits for next $Time"
Write-Host "  Script: $ScriptPath"
Write-Host ""
Write-Host 'Test now: .\deploy\azure\pull-archive-and-merge.ps1'
