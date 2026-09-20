# One-shot from Windows laptop: open NSG 80/443 and enable self-signed HTTPS on the VM.
# No domain required. Browser will warn once → Advanced → Proceed.
#
# Usage (repo root, after az login):
#   .\deploy\azure\enable-https-remote.ps1
#
# Syncs Caddy/compose HTTPS files to the VM, then runs enable-https.sh (selfsigned).
#
param(
    [string]$VmHost,
    [string]$VmUser,
    [string]$VmProjectDir,
    [string]$SshKeyPath,
    [string]$SourceIp,
    [switch]$SkipPortOpen,
    [ValidateSet("selfsigned", "acme")]
    [string]$Mode = "selfsigned"
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent (Split-Path -Parent $DeployDir)
$ConfigFile = Join-Path $DeployDir "laptop.config.ps1"
if (Test-Path $ConfigFile) { . $ConfigFile }

function Resolve-Param([string]$Value, [string]$Default) {
    if ($Value) { return $Value }
    if ($Default) { return $Default }
    return $null
}

$VmHost       = Resolve-Param $VmHost       $script:VmHost
$VmUser       = Resolve-Param $VmUser       $(if ($script:VmUser) { $script:VmUser } else { "azureuser" })
$VmProjectDir = Resolve-Param $VmProjectDir $(if ($script:VmProjectDir) { $script:VmProjectDir } else { "/home/azureuser/options_advisor_system" })
$SshKeyPath   = Resolve-Param $SshKeyPath   $script:SshKeyPath
$SourceIp     = Resolve-Param $SourceIp     $script:AzureNsgSource

if (-not $VmHost -or $VmHost -eq "YOUR_VM_PUBLIC_IP") {
    throw "Set VmHost in deploy\azure\laptop.config.ps1 or pass -VmHost"
}

$sshTarget = "${VmUser}@${VmHost}"
$sshArgs = @("-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20")
$scpArgs = @("-o", "StrictHostKeyChecking=accept-new")
if ($SshKeyPath) {
    $sshArgs += @("-i", $SshKeyPath)
    $scpArgs += @("-i", $SshKeyPath)
}

Write-Host "==> [1/3] Syncing HTTPS files to VM..."
& ssh @sshArgs $sshTarget "mkdir -p '$VmProjectDir/deploy/caddy' '$VmProjectDir/deploy/azure'"

$files = @(
    @{ Local = "docker-compose.yml"; Remote = "docker-compose.yml" },
    @{ Local = "deploy\caddy\Caddyfile"; Remote = "deploy/caddy/Caddyfile" },
    @{ Local = "deploy\caddy\Caddyfile.selfsigned"; Remote = "deploy/caddy/Caddyfile.selfsigned" },
    @{ Local = "deploy\caddy\Caddyfile.acme"; Remote = "deploy/caddy/Caddyfile.acme" },
    @{ Local = "deploy\azure\enable-https.sh"; Remote = "deploy/azure/enable-https.sh" },
    @{ Local = "deploy\azure\open-port-https.sh"; Remote = "deploy/azure/open-port-https.sh" },
    @{ Local = "deploy\load-compose-profiles.sh"; Remote = "deploy/load-compose-profiles.sh" }
)
foreach ($f in $files) {
    $local = Join-Path $RepoRoot $f.Local
    if (-not (Test-Path $local)) { throw "Missing local file: $local" }
    & scp @scpArgs $local "${sshTarget}:$VmProjectDir/$($f.Remote)"
}
& ssh @sshArgs $sshTarget "sed -i 's/\r$//' '$VmProjectDir/deploy/azure/enable-https.sh' '$VmProjectDir/deploy/azure/open-port-https.sh' '$VmProjectDir/deploy/load-compose-profiles.sh' '$VmProjectDir/deploy/caddy/Caddyfile' '$VmProjectDir/deploy/caddy/Caddyfile.selfsigned' '$VmProjectDir/deploy/caddy/Caddyfile.acme' 2>/dev/null; chmod +x '$VmProjectDir/deploy/azure/enable-https.sh' '$VmProjectDir/deploy/azure/open-port-https.sh'"

if (-not $SkipPortOpen) {
    Write-Host "==> [2/3] Opening Azure NSG ports 80 + 443..."
    $portArgs = @{ VmHost = $VmHost }
    if ($SourceIp) { $portArgs.SourceIp = $SourceIp }
    & (Join-Path $DeployDir "open-port-https.ps1") @portArgs
} else {
    Write-Host "==> [2/3] Skipped NSG open (-SkipPortOpen)"
}

Write-Host "==> [3/3] Enabling HTTPS on VM (mode=$Mode)..."
$remote = @"
set -euo pipefail
cd '$VmProjectDir'
export HTTPS_MODE='$Mode'
export FORCE_PUBLIC_IP='$VmHost'
chmod +x deploy/azure/enable-https.sh
./deploy/azure/enable-https.sh
"@
& ssh @sshArgs $sshTarget $remote

Write-Host ""
Write-Host "Done. Open: https://${VmHost}/"
Write-Host "Browser warning (self-signed): Advanced → Proceed."
Write-Host "Kite redirect (if needed): https://${VmHost}/zerodha/callback"

if (-not $SkipPortOpen) {
    Write-Host "==> Closing public NSG :5001 (HTTPS-only)..."
    $closeArgs = @{ VmHost = $VmHost }
    & (Join-Path $DeployDir "close-port-5001.ps1") @closeArgs
}
