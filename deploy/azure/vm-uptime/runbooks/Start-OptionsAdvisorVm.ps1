param(
    [string]$ResourceGroupName = "STOCKAPPS",
    [string]$VMName = "OptionsAdvisor"
)

$ErrorActionPreference = "Stop"

Write-Output "Starting VM '$VMName' in RG '$ResourceGroupName'..."

Import-Module Az.Accounts -ErrorAction Stop
Import-Module Az.Compute -ErrorAction Stop
Connect-AzAccount -Identity -ErrorAction Stop | Out-Null

$vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -Status -ErrorAction Stop
$powerState = ($vm.Statuses | Where-Object { $_.Code -like "PowerState/*" }).Code
Write-Output "Current power state: $powerState"

if ($powerState -eq "PowerState/running") {
    Write-Output "VM already running."
    exit 0
}

Start-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -ErrorAction Stop | Out-Null

$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 10
    $vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -Status
    $powerState = ($vm.Statuses | Where-Object { $_.Code -like "PowerState/*" }).Code
    Write-Output "Waiting for start... $powerState"
} while ($powerState -ne "PowerState/running" -and (Get-Date) -lt $deadline)

if ($powerState -ne "PowerState/running") {
    throw "VM did not reach running within 5 minutes (last state: $powerState)"
}

Write-Output "VM running successfully."
