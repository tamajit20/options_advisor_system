# Configure Azure Automation start/stop schedules for the Options Advisor VM.
#
# Creates (or updates) an Automation account, runbooks, and Mon–Fri schedules aligned
# with the app job windows (IST → UTC). Stopping uses deallocate to reduce compute cost.
#
# Schedule (IST → UTC):
#   Start market  08:55 Mon–Fri  →  03:25 UTC
#   Stop market   16:10 Mon–Fri  →  10:40 UTC  (Fri weekly_cleanup @ 15:40)
# Evening EOD window removed — morning_eod_catchup @ 09:00 in the app.
#
# Prerequisites (Windows laptop):
#   winget install Microsoft.AzureCLI
#   az login
#   (Optional) Import Az modules into the Automation account — see -ImportAzModules
#
# Usage (from repo root):
#   .\deploy\azure\VMUpTimeConfiguration.ps1
#   .\deploy\azure\VMUpTimeConfiguration.ps1 -WhatIf
#   .\deploy\azure\VMUpTimeConfiguration.ps1 -ImportAzModules
#   .\deploy\azure\VMUpTimeConfiguration.ps1 -Remove
#
param(
    [string]$ResourceGroupName,
    [string]$VmName,
    [string]$AutomationAccountName,
    [string]$Location,
    [string]$SubscriptionId,
    [ValidateSet("PowerShell", "Python3")]
    [string]$RunbookType,
    [switch]$ImportAzModules,
    [switch]$Remove,
    [switch]$WhatIf
)

$ErrorActionPreference = "Continue"

$azCliDir = "${env:ProgramFiles}\Microsoft SDKs\Azure\CLI2\wbin"
if ((Test-Path $azCliDir) -and ($env:Path -notlike "*$azCliDir*")) {
    $env:Path = "$azCliDir;$env:Path"
}

$DeployDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot    = Split-Path -Parent (Split-Path -Parent $DeployDir)
$UptimeDir   = Join-Path $DeployDir "vm-uptime"
$RunbookDir  = Join-Path $UptimeDir "runbooks"

$LaptopConfig = Join-Path $DeployDir "laptop.config.ps1"
$UptimeConfig = Join-Path $UptimeDir "vm-uptime.config.ps1"
if (Test-Path $LaptopConfig) { . $LaptopConfig }
if (Test-Path $UptimeConfig) { . $UptimeConfig }

function Resolve-ConfigValue {
    param(
        [string]$ParamValue,
        [string]$ScriptValue,
        [string]$DefaultValue = ""
    )
    if ($ParamValue) { return $ParamValue }
    if ($ScriptValue) { return $ScriptValue }
    return $DefaultValue
}

$ResourceGroupName     = Resolve-ConfigValue $ResourceGroupName $script:AzureResourceGroup "STOCKAPPS"
$VmName                = Resolve-ConfigValue $VmName $script:AzureVmName "OptionsAdvisor"
$AutomationAccountName = Resolve-ConfigValue $AutomationAccountName $script:AutomationAccountName "aa-stockapps-optionsadvisor"
$RunbookType           = Resolve-ConfigValue $RunbookType $script:AutomationRunbookType "PowerShell"

$RunbookStartName = "Start-OptionsAdvisorVm"
$RunbookStopName  = "Stop-OptionsAdvisorVm"
$RunbookStartPath = Join-Path $RunbookDir "$RunbookStartName.ps1"
$RunbookStopPath  = Join-Path $RunbookDir "$RunbookStopName.ps1"

$WeekDays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

# IST job windows converted to UTC (Azure Automation schedules use UTC).
$ScheduleDefinitions = @(
    @{
        Name        = "sched-oa-start-market-mf"
        Runbook     = $RunbookStartName
        HourUtc     = 3
        MinuteUtc   = 25
        Description = "Mon-Fri 08:55 IST - start VM for market session"
    },
    @{
        Name        = "sched-oa-stop-market-mf"
        Runbook     = $RunbookStopName
        HourUtc     = 10
        MinuteUtc   = 40
        Description = "Mon-Fri 16:10 IST - stop VM after market session (Fri weekly_cleanup @ 15:40)"
    }
)

# Retired schedules (removed from config — deleted on each apply).
$RetiredScheduleNames = @(
    "sched-oa-start-eod-mf",
    "sched-oa-stop-eod-mf"
)

function Get-AzCliPath {
    $cmd = Get-Command az -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $default = "${env:ProgramFiles}\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
    if (Test-Path $default) { return $default }
    return $null
}

function Invoke-AzCliRaw {
    param([string[]]$CliArgs)
    $az = Get-AzCliPath
    if (-not $az) {
        throw "Azure CLI not found. Install: winget install Microsoft.AzureCLI"
    }
    & $az @CliArgs
    return $LASTEXITCODE
}

function Assert-AzCli {
    $az = Get-AzCliPath
    if (-not $az) {
        throw "Azure CLI not found. Install: winget install Microsoft.AzureCLI"
    }
    $account = & $az account show 2>$null | ConvertFrom-Json
    if (-not $account) {
        throw "Not logged in to Azure. Run: az login"
    }
    return $account
}

function Get-SubscriptionId([object]$Account) {
    if ($SubscriptionId) { return $SubscriptionId }
    return $Account.id
}

function Get-VmLocation {
    param([string]$Rg, [string]$Name)
    $loc = az vm show -g $Rg -n $Name --query "location" -o tsv 2>$null
    if (-not $loc) {
        throw "VM '$Name' not found in resource group '$Rg'. Check name/RG or run: az login"
    }
    return $loc
}

function Get-NextUtcStartTime {
    param([int]$HourUtc, [int]$MinuteUtc)
    $now = [DateTime]::UtcNow
    $start = [DateTime]::SpecifyKind(
        [DateTime]::new($now.Year, $now.Month, $now.Day, $HourUtc, $MinuteUtc, 0),
        [DateTimeKind]::Utc
    )
    if ($start -le $now) {
        $start = $start.AddDays(1)
    }
    return $start.ToString("yyyy-MM-ddTHH:mm:ss")
}

function Invoke-AzCli {
    param([string[]]$CliArgs, [string]$Label)
    if ($WhatIf) {
        Write-Host "[WhatIf] $Label"
        Write-Host "         az $($CliArgs -join ' ')"
        return $null
    }
    Write-Host "==> $Label"
    $code = Invoke-AzCliRaw -CliArgs $CliArgs
    if ($code -ne 0) {
        throw "Azure CLI failed: az $($CliArgs -join ' ')"
    }
}

function Get-AutomationAccount {
    param([string]$Rg, [string]$Name)
    $json = az automation account show -g $Rg -n $Name -o json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return $null }
    return $json | ConvertFrom-Json
}

function Ensure-AutomationAccount {
    param([string]$Rg, [string]$Name, [string]$Loc)

    $existing = Get-AutomationAccount -Rg $Rg -Name $Name
    if ($existing) {
        Write-Host "    Automation account '$Name' already exists."
        return $existing
    }

    Invoke-AzCli @(
        "automation", "account", "create",
        "-g", $Rg,
        "-n", $Name,
        "--location", $Loc,
        "--sku", "Basic"
    ) -Label "Creating Automation account '$Name' in $Loc..."

    return Get-AutomationAccount -Rg $Rg -Name $Name
}

function Ensure-SystemIdentity {
    param([string]$Rg, [string]$Name)

    $aa = Get-AutomationAccount -Rg $Rg -Name $Name
    if ($aa.identity.type -eq "SystemAssigned" -and $aa.identity.principalId) {
        Write-Host "    System-assigned managed identity already enabled."
        return $aa.identity.principalId
    }

    Invoke-AzCli @(
        "resource", "update",
        "--resource-group", $Rg,
        "--name", $Name,
        "--resource-type", "Microsoft.Automation/automationAccounts",
        "--set", "identity.type=SystemAssigned"
    ) -Label "Enabling system-assigned managed identity on '$Name'..."

    Start-Sleep -Seconds 5
    $aa = Get-AutomationAccount -Rg $Rg -Name $Name
    return $aa.identity.principalId
}

function Ensure-VmContributorRole {
    param(
        [string]$SubId,
        [string]$PrincipalId,
        [string]$ScopeRg
    )

    $scope = "/subscriptions/$SubId/resourceGroups/$ScopeRg"
    $existing = az role assignment list `
        --assignee $PrincipalId `
        --role "Virtual Machine Contributor" `
        --scope $scope `
        -o json 2>$null | ConvertFrom-Json

    if ($existing -and $existing.Count -gt 0) {
        Write-Host "    Role 'Virtual Machine Contributor' already assigned on $ScopeRg."
        return
    }

    Invoke-AzCli @(
        "role", "assignment", "create",
        "--assignee-object-id", $PrincipalId,
        "--assignee-principal-type", "ServicePrincipal",
        "--role", "Virtual Machine Contributor",
        "--scope", $scope
    ) -Label "Assigning 'Virtual Machine Contributor' on resource group '$ScopeRg'..."
}

function Ensure-Runbook {
    param(
        [string]$Rg,
        [string]$AutomationAccount,
        [string]$RunbookName,
        [string]$ContentPath,
        [string]$Type
    )

    if (-not (Test-Path $ContentPath)) {
        throw "Runbook file not found: $ContentPath"
    }

    $existing = az automation runbook show `
        -g $Rg `
        --automation-account-name $AutomationAccount `
        -n $RunbookName `
        -o json 2>$null | ConvertFrom-Json

    if (-not $existing) {
        Invoke-AzCli @(
            "automation", "runbook", "create",
            "-g", $Rg,
            "--automation-account-name", $AutomationAccount,
            "-n", $RunbookName,
            "--location", $script:VmLocation,
            "--type", $Type
        ) -Label "Creating runbook '$RunbookName'..."
    }

    $content = Get-Content -Path $ContentPath -Raw -Encoding UTF8
    if (-not $content.Trim()) {
        throw "Runbook file is empty: $ContentPath"
    }
    if ($WhatIf) {
        Write-Host "[WhatIf] Upload runbook content: $RunbookName"
    } else {
        Write-Host "==> Publishing runbook content: $RunbookName"
        az automation runbook replace-content `
            -g $Rg `
            --automation-account-name $AutomationAccount `
            -n $RunbookName `
            --content "@$ContentPath" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to upload runbook content for $RunbookName"
        }
        az automation runbook publish `
            -g $Rg `
            --automation-account-name $AutomationAccount `
            -n $RunbookName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to publish runbook $RunbookName"
        }
    }
}

function Ensure-Schedule {
    param(
        [string]$Rg,
        [string]$AutomationAccount,
        [hashtable]$Definition
    )

    $startTime = Get-NextUtcStartTime -HourUtc $Definition.HourUtc -MinuteUtc $Definition.MinuteUtc
    $weekDaysArg = ($WeekDays -join " ")

    $existing = az automation schedule show `
        -g $Rg `
        --automation-account-name $AutomationAccount `
        -n $Definition.Name `
        -o json 2>$null | ConvertFrom-Json

    if ($existing) {
        Write-Host "    Schedule '$($Definition.Name)' already exists - skipping create."
        return
    }

    Invoke-AzCli @(
        "automation", "schedule", "create",
        "-g", $Rg,
        "--automation-account-name", $AutomationAccount,
        "-n", $Definition.Name,
        "--frequency", "Week",
        "--interval", "1",
        "--start-time", $startTime,
        "--time-zone", "UTC",
        "--week-days", $weekDaysArg,
        "--description", $Definition.Description
    ) -Label "Creating schedule '$($Definition.Name)' ($($Definition.Description))..."
}

function Ensure-JobScheduleLink {
    param(
        [string]$Rg,
        [string]$AutomationAccount,
        [string]$ScheduleName,
        [string]$RunbookName,
        [string]$ResourceGroupName,
        [string]$VmName
    )

    $linkName = "link-$ScheduleName"

    $existing = az automation job-schedule list `
        -g $Rg `
        --automation-account-name $AutomationAccount `
        -o json 2>$null | ConvertFrom-Json

    $alreadyLinked = $false
    if ($existing) {
        foreach ($item in $existing) {
            if ($item.schedule.name -eq $ScheduleName -and $item.runbook.name -eq $RunbookName) {
                $alreadyLinked = $true
                break
            }
        }
    }

    if ($alreadyLinked) {
        Write-Host "    Job schedule link for '$ScheduleName' -> '$RunbookName' already exists."
        return
    }

    if ($WhatIf) {
        Write-Host "[WhatIf] Link schedule '$ScheduleName' to runbook '$RunbookName'"
        return
    }

    Write-Host "==> Linking schedule '$ScheduleName' -> runbook '$RunbookName'..."
    az automation job-schedule create `
        -g $Rg `
        --automation-account-name $AutomationAccount `
        --schedule-name $ScheduleName `
        --runbook-name $RunbookName `
        --parameters `
            "ResourceGroupName=$ResourceGroupName" `
            "VMName=$VmName" | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to link schedule '$ScheduleName' to runbook '$RunbookName'"
    }
}

function Import-AutomationAzModule {
    param(
        [string]$Rg,
        [string]$AutomationAccount,
        [string]$ModuleName,
        [string]$ModuleVersion = "5.4.0"
    )

    $existing = az automation module show `
        -g $Rg `
        --automation-account-name $AutomationAccount `
        -n $ModuleName `
        -o json 2>$null | ConvertFrom-Json

    if ($existing -and $existing.provisioningState -eq "Succeeded") {
        Write-Host "    Module '$ModuleName' already imported."
        return
    }

    $uri = "https://www.powershellgallery.com/api/v2/package/$ModuleName/$ModuleVersion"
    Invoke-AzCli @(
        "automation", "module", "create",
        "-g", $Rg,
        "--automation-account-name", $AutomationAccount,
        "-n", $ModuleName,
        "--content-link", "uri=$uri"
    ) -Label "Importing module '$ModuleName' $ModuleVersion (may take 15-30 min)..."

    Write-Host "    Module import started. Check Automation account -> Modules until state = Succeeded."
}

function Remove-ScheduleAndJobLinks {
    param(
        [string]$Rg,
        [string]$AutomationAccount,
        [string]$ScheduleName
    )

    if ($WhatIf) {
        Write-Host "[WhatIf] Remove retired schedule + links: $ScheduleName"
        return
    }

    $links = az automation job-schedule list `
        -g $Rg `
        --automation-account-name $AutomationAccount `
        -o json 2>$null | ConvertFrom-Json

    if ($links) {
        foreach ($link in $links) {
            if ($link.schedule.name -eq $ScheduleName) {
                az automation job-schedule delete `
                    -g $Rg `
                    --automation-account-name $AutomationAccount `
                    --job-schedule-id $link.jobScheduleId | Out-Null
            }
        }
    }

    az automation schedule delete `
        -g $Rg `
        --automation-account-name $AutomationAccount `
        -n $ScheduleName `
        --yes 2>$null | Out-Null

    Write-Host "    Removed retired schedule: $ScheduleName"
}

function Remove-UptimeConfiguration {
    param(
        [string]$Rg,
        [string]$AutomationAccount
    )

    Write-Host "==> Removing VM uptime schedules and links from '$AutomationAccount'..."

    foreach ($def in $ScheduleDefinitions) {
        if ($WhatIf) {
            Write-Host "[WhatIf] Remove job-schedule link + schedule: $($def.Name)"
            continue
        }

        $links = az automation job-schedule list `
            -g $Rg `
            --automation-account-name $AutomationAccount `
            -o json 2>$null | ConvertFrom-Json

        if ($links) {
            foreach ($link in $links) {
                if ($link.schedule.name -eq $def.Name) {
                    $jobScheduleId = $link.jobScheduleId
                    az automation job-schedule delete `
                        -g $Rg `
                        --automation-account-name $AutomationAccount `
                        --job-schedule-id $jobScheduleId | Out-Null
                }
            }
        }

        az automation schedule delete `
            -g $Rg `
            --automation-account-name $AutomationAccount `
            -n $def.Name `
            --yes 2>$null | Out-Null
    }

    foreach ($rb in @($RunbookStartName, $RunbookStopName)) {
        if ($WhatIf) {
            Write-Host "[WhatIf] Remove runbook: $rb"
            continue
        }
        az automation runbook delete `
            -g $Rg `
            --automation-account-name $AutomationAccount `
            -n $rb `
            --yes 2>$null | Out-Null
    }

    Write-Host "Done. Automation account '$AutomationAccount' was not deleted (remove manually if unused)."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Options Advisor - VM uptime configuration"
Write-Host "  VM:              $VmName"
Write-Host "  Resource group:  $ResourceGroupName"
Write-Host "  Automation acct: $AutomationAccountName"
Write-Host ""

$account = Assert-AzCli
$subId = Get-SubscriptionId $account
$script:VmLocation = if ($Location) { $Location } else { Get-VmLocation -Rg $ResourceGroupName -Name $VmName }
Write-Host "  Location:        $script:VmLocation"
Write-Host "  Subscription:    $($account.name)"
Write-Host ""

if ($Remove) {
    $aa = Get-AutomationAccount -Rg $ResourceGroupName -Name $AutomationAccountName
    if (-not $aa) {
        Write-Host "Automation account '$AutomationAccountName' not found - nothing to remove."
        exit 0
    }
    Remove-UptimeConfiguration -Rg $ResourceGroupName -AutomationAccount $AutomationAccountName
    exit 0
}

Ensure-AutomationAccount -Rg $ResourceGroupName -Name $AutomationAccountName -Loc $script:VmLocation | Out-Null
$principalId = Ensure-SystemIdentity -Rg $ResourceGroupName -Name $AutomationAccountName
Ensure-VmContributorRole -SubId $subId -PrincipalId $principalId -ScopeRg $ResourceGroupName

if ($ImportAzModules) {
    Import-AutomationAzModule -Rg $ResourceGroupName -AutomationAccount $AutomationAccountName -ModuleName "Az.Accounts"
    Import-AutomationAzModule -Rg $ResourceGroupName -AutomationAccount $AutomationAccountName -ModuleName "Az.Compute"
} else {
    Write-Host ""
    Write-Host "Tip: First-time setup may require Az modules in the Automation account."
    Write-Host "     Re-run with -ImportAzModules (takes ~15-30 min) or import in Azure Portal."
    Write-Host ""
}

Ensure-Runbook -Rg $ResourceGroupName -AutomationAccount $AutomationAccountName `
    -RunbookName $RunbookStartName -ContentPath $RunbookStartPath -Type $RunbookType
Ensure-Runbook -Rg $ResourceGroupName -AutomationAccount $AutomationAccountName `
    -RunbookName $RunbookStopName -ContentPath $RunbookStopPath -Type $RunbookType

foreach ($def in $ScheduleDefinitions) {
    Ensure-Schedule -Rg $ResourceGroupName -AutomationAccount $AutomationAccountName -Definition $def
    Ensure-JobScheduleLink `
        -Rg $ResourceGroupName `
        -AutomationAccount $AutomationAccountName `
        -ScheduleName $def.Name `
        -RunbookName $def.Runbook `
        -ResourceGroupName $ResourceGroupName `
        -VmName $VmName
}

foreach ($retired in $RetiredScheduleNames) {
    Remove-ScheduleAndJobLinks `
        -Rg $ResourceGroupName `
        -AutomationAccount $AutomationAccountName `
        -ScheduleName $retired
}

Write-Host ""
Write-Host "Done. VM uptime schedules configured."
Write-Host ""
Write-Host "Verify in Azure Portal:"
Write-Host "  Automation account -> $AutomationAccountName -> Schedules / Runbooks / Jobs"
Write-Host "  Virtual machines  -> $VmName -> Status should be 'Stopped (deallocated)' when off"
Write-Host ""
Write-Host "Manual test (Portal -> Runbooks -> Start test):"
Write-Host "  1. Start-OptionsAdvisorVm  - VM should reach Running"
Write-Host "  2. Stop-OptionsAdvisorVm   - VM should show Stopped (deallocated)"
Write-Host ""
Write-Host "Schedule summary (UTC to IST):"
Write-Host "  sched-oa-start-market-mf  03:25 UTC  =  08:55 IST  Mon-Fri"
Write-Host "  sched-oa-stop-market-mf   10:40 UTC  =  16:10 IST  Mon-Fri"
Write-Host "  (evening EOD schedules removed - morning catchup @ 09:00 in app)"
Write-Host ""
