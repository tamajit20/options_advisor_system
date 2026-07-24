# Azure Automation runbook — stop and DEALLOCATE Options Advisor VM (saves compute cost).
# Deployed by deploy/azure/VMUpTimeConfiguration.ps1
#
param(
    [Parameter(Mandatory = $false)]
    [string]$ResourceGroupName = "STOCKAPPS",

    [Parameter(Mandatory = $false)]
    [string]$VMName = "OptionsAdvisor"
)

$ErrorActionPreference = "Stop"

Write-Output "Stopping (deallocating) VM '$VMName' in resource group '$ResourceGroupName'..."

Connect-AzAccount -Identity | Out-Null

$vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -Status
$powerState = ($vm.Statuses | Where-Object { $_.Code -like "PowerState/*" }).Code

if ($powerState -eq "PowerState/deallocated") {
    Write-Output "VM is already deallocated — no action taken."
    exit 0
}

Stop-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -Force -NoWait | Out-Null
Write-Output "Stop (deallocate) request submitted for VM '$VMName'."
