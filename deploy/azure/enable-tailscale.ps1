# Join the laptop and the VM to Tailscale, point the dashboard at the VM's
# tailnet address, then close public inbound 22/80/443/5001.
#
# Requires TailscaleAuthKey in deploy/azure/laptop.config.ps1
# (Tailscale admin -> Settings -> Keys -> reusable, not ephemeral).
#
#   .\deploy\azure\enable-tailscale.ps1
#   .\deploy\azure\enable-tailscale.ps1 -LaptopOnly
#
param(
    [switch]$LaptopOnly,
    [switch]$SkipLockdown
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $DeployDir)
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"
if (-not (Test-Path $ConfigFile)) {
    throw "Missing $ConfigFile - copy laptop.config.ps1.example and set TailscaleAuthKey."
}
. $ConfigFile

function Set-ConfigAssignment {
    param([string]$Path, [string]$Name, [string]$Value)
    $text = [IO.File]::ReadAllText($Path)
    $pattern = "(?m)^(\`$script:$Name\s*=\s*)`"[^`"]*`""
    if ([regex]::IsMatch($text, $pattern)) {
        $text = [regex]::Replace($text, $pattern, "`${1}`"$Value`"")
    } else {
        $suffix = "`r`n`$script:$Name = `"$Value`"`r`n"
        if (-not $text.EndsWith("`n")) { $text += "`r`n" }
        $text += $suffix
    }
    [IO.File]::WriteAllText($Path, $text)
}

function Get-TailscaleExe {
    foreach ($c in @(
        "$env:ProgramFiles\Tailscale\tailscale.exe",
        "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe"
    )) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

$key = if ($script:TailscaleAuthKey) { [string]$script:TailscaleAuthKey.Trim() } else { "" }
if (-not $key -or $key -eq "tskey-auth-REPLACE_ME") {
    throw "Set TailscaleAuthKey in deploy\azure\laptop.config.ps1. Create a reusable auth key in the Tailscale admin console (Settings -> Keys). Do not commit it."
}

$ts = Get-TailscaleExe
if (-not $ts) {
    Write-Host "==> Installing Tailscale on this laptop..."
    winget install --id Tailscale.Tailscale -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget install Tailscale.Tailscale failed" }
    $ts = Get-TailscaleExe
    if (-not $ts) { throw "Tailscale installed but tailscale.exe was not found." }
}

Write-Host "==> Joining this laptop to Tailscale..."
& $ts up --auth-key=$key --hostname=$env:COMPUTERNAME --accept-dns=true --unattended --reset
if ($LASTEXITCODE -ne 0) { throw "tailscale up failed on the laptop" }

if ($LaptopOnly) {
    Write-Host "Laptop is on Tailscale. VM join was not requested."
    exit 0
}

$VmHost = $script:VmHost
$VmUser = if ($script:VmUser) { $script:VmUser } else { "azureuser" }
$VmProjectDir = if ($script:VmProjectDir) { $script:VmProjectDir } else { "/home/azureuser/options_advisor_system" }
$SshKeyPath = $script:SshKeyPath
$sshTarget = "${VmUser}@${VmHost}"
if (-not $VmHost -or $VmHost -eq "YOUR_VM_PUBLIC_IP") { throw "Set VmHost before joining the VM." }
if (-not $SshKeyPath -or -not (Test-Path $SshKeyPath)) { throw "SshKeyPath is missing." }

$sshArgs = @("-i", $SshKeyPath, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")
$scpArgs = @("-i", $SshKeyPath, "-o", "StrictHostKeyChecking=accept-new")

$localScript = Join-Path $DeployDir "install-tailscale.sh"
$localHttps = Join-Path $DeployDir "enable-https.sh"
Write-Host "==> Installing Tailscale on the VM..."
& scp @scpArgs $localScript "${sshTarget}:${VmProjectDir}/deploy/azure/install-tailscale.sh"
if ($LASTEXITCODE -ne 0) { throw "scp install-tailscale.sh failed" }
& scp @scpArgs $localHttps "${sshTarget}:${VmProjectDir}/deploy/azure/enable-https.sh"
if ($LASTEXITCODE -ne 0) { throw "scp enable-https.sh failed" }

$keyFile = Join-Path $env:TEMP ("ts-auth-" + [guid]::NewGuid().ToString("n"))
[IO.File]::WriteAllText($keyFile, $key)
try {
    & scp @scpArgs $keyFile "${sshTarget}:/tmp/ts-auth-key"
    if ($LASTEXITCODE -ne 0) { throw "scp auth key failed" }
} finally {
    Remove-Item -Force $keyFile -ErrorAction SilentlyContinue
}

$remoteJoin = "sed -i 's/\r$//' '$VmProjectDir/deploy/azure/install-tailscale.sh' '$VmProjectDir/deploy/azure/enable-https.sh' && chmod +x '$VmProjectDir/deploy/azure/install-tailscale.sh' && bash '$VmProjectDir/deploy/azure/install-tailscale.sh' /tmp/ts-auth-key && rm -f /tmp/ts-auth-key"
$joinOut = & ssh @sshArgs $sshTarget $remoteJoin
if ($LASTEXITCODE -ne 0) { throw "VM Tailscale join failed" }
$tsIp = ($joinOut | Where-Object { $_ -match '^TAILSCALE_IP=' } | Select-Object -Last 1)
if ($tsIp) { $tsIp = $tsIp.Substring("TAILSCALE_IP=".Length).Trim() }
if (-not $tsIp -or $tsIp -notmatch '^100\.') {
    throw "VM did not report a Tailscale IPv4 address."
}
Write-Host "    VM Tailscale address: $tsIp"

Write-Host "==> Refreshing the dashboard certificate for the tailnet address..."
$remoteHttps = "cd '$VmProjectDir' && chmod +x deploy/azure/enable-https.sh && ./deploy/azure/enable-https.sh"
& ssh @sshArgs $sshTarget $remoteHttps
if ($LASTEXITCODE -ne 0) { throw "enable-https.sh failed after Tailscale join" }

Write-Host "==> Checking SSH and HTTPS over Tailscale before closing public ports..."
$tsTarget = "${VmUser}@${tsIp}"
$ready = $false
foreach ($try in 1..8) {
    & ssh @sshArgs $tsTarget "echo ts_ok" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 3
}
if (-not $ready) {
    throw "SSH to ${tsIp} failed. Public ports were left open."
}
$code = (& curl.exe -k -s -o NUL -w "%{http_code}" --max-time 20 "https://${tsIp}/")
if ($code) { $code = $code.ToString().Trim() }
if ($LASTEXITCODE -ne 0 -or $code -notmatch '^[0-9]{3}$' -or $code -eq "000") {
    throw "Dashboard over Tailscale did not answer (curl exit $LASTEXITCODE, HTTP $code). Public ports were left open."
}

if (-not $SkipLockdown) {
    Write-Host "==> Marking the VM so later installs do not reopen public ports..."
    & ssh @sshArgs $tsTarget "mkdir -p '$VmProjectDir/data' && printf '%s\n' locked > '$VmProjectDir/data/tailscale_lockdown'"
    if ($LASTEXITCODE -ne 0) { throw "Could not write tailscale_lockdown marker" }

    & (Join-Path $DeployDir "close-public-inbound.ps1")
    if ($LASTEXITCODE -ne 0) { throw "close-public-inbound.ps1 failed" }
}

$publicHost = if ($script:PublicVmHost) { $script:PublicVmHost } else { "" }
if (-not $publicHost -and $VmHost -notmatch '^100\.') {
    Set-ConfigAssignment -Path $ConfigFile -Name "PublicVmHost" -Value $VmHost
}
Set-ConfigAssignment -Path $ConfigFile -Name "VmHost" -Value $tsIp

Write-Host ""
Write-Host "Tailscale is the way in."
Write-Host "  Dashboard:  https://${tsIp}/"
Write-Host "  SSH:        ssh -i `"$SshKeyPath`" ${tsTarget}"
Write-Host "Install the Tailscale app on the phone and sign in to the same tailnet."
Write-Host "Zerodha app redirect URL must be https://${tsIp}/zerodha/callback"
