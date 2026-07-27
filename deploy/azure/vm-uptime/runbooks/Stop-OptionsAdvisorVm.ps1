param(
    [string]$ResourceGroupName = "STOCKAPPS",
    [string]$VMName = "OptionsAdvisor"
)

$ErrorActionPreference = "Stop"

Write-Output "Stopping (deallocating) VM '$VMName' in RG '$ResourceGroupName'..."

Import-Module Az.Accounts -ErrorAction Stop
Import-Module Az.Compute -ErrorAction Stop
Connect-AzAccount -Identity -ErrorAction Stop | Out-Null

$vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -Status -ErrorAction Stop
$powerState = ($vm.Statuses | Where-Object { $_.Code -like "PowerState/*" }).Code
Write-Output "Current power state: $powerState"

if ($powerState -eq "PowerState/deallocated") {
    Write-Output "VM already deallocated."
    exit 0
}

Stop-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -Force -ErrorAction Stop | Out-Null

$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 10
    $vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -Status
    $powerState = ($vm.Statuses | Where-Object { $_.Code -like "PowerState/*" }).Code
    Write-Output "Waiting for deallocate... $powerState"
} while ($powerState -ne "PowerState/deallocated" -and (Get-Date) -lt $deadline)

if ($powerState -ne "PowerState/deallocated") {
    throw "VM did not deallocate within 5 minutes (last state: $powerState)"
}

Write-Output "VM deallocated successfully."
