# Azure Automation runbook — start Options Advisor VM (deallocated → running).
# Deployed by deploy/azure/VMUpTimeConfiguration.ps1
#
param(
    [Parameter(Mandatory = $false)]
    [string]$ResourceGroupName = "STOCKAPPS",

    [Parameter(Mandatory = $false)]
    [string]$VMName = "OptionsAdvisor"
)

$ErrorActionPreference = "Stop"

Write-Output "Starting VM '$VMName' in resource group '$ResourceGroupName'..."

Connect-AzAccount -Identity | Out-Null

$vm = Get-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -Status
$powerState = ($vm.Statuses | Where-Object { $_.Code -like "PowerState/*" }).Code

if ($powerState -eq "PowerState/running") {
    Write-Output "VM is already running — no action taken."
    exit 0
}

Start-AzVM -ResourceGroupName $ResourceGroupName -Name $VMName -NoWait | Out-Null
Write-Output "Start request submitted for VM '$VMName'."
