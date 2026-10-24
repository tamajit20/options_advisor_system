# Full first-time install on Azure VM from your Windows laptop:
#   SSH -> vm-install-deploy.sh on VM -> Tailscale -> close public inbound 22/80/443/5001
#
# Prerequisites:
#   - SSH key + VM IP in deploy/azure/laptop.config.ps1
#   - .env.docker on laptop (uploaded to VM) OR already present on VM
#   - Azure CLI logged in on laptop (for NSG): az login
#
# Usage (from repo root):
#   copy deploy\azure\laptop.config.ps1.example deploy\azure\laptop.config.ps1
#   notepad deploy\azure\laptop.config.ps1
#   .\deploy\azure\remote-vm-install.ps1
#   .\deploy\azure\remote-vm-install.ps1 -EnvFile "D:\path\.env.docker"
#
param(
    [string]$VmHost,
    [string]$VmUser,
    [string]$VmProjectDir,
    [string]$SshKeyPath,
    [string]$EnvFile,
    [string]$SourceIp,
    [switch]$SkipPortOpen,
    [switch]$UseExistingDb,
    [switch]$FreshDb
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
if (-not $SkipPortOpen) {
    $tsKey = if ($script:TailscaleAuthKey) { [string]$script:TailscaleAuthKey.Trim() } else { "" }
    if (-not $tsKey -or $tsKey -eq "tskey-auth-REPLACE_ME") {
        throw "Set TailscaleAuthKey in deploy\azure\laptop.config.ps1 before the first install. This script closes public SSH at the end, so the key has to be ready first."
    }
}

$sshTarget = "${VmUser}@${VmHost}"
$sshArgs = @("-o", "StrictHostKeyChecking=accept-new")
$scpArgs = @()
if ($SshKeyPath) {
    $sshArgs += @("-i", $SshKeyPath)
    $scpArgs += @("-i", $SshKeyPath)
}

$installFlags = @()
if ($FreshDb) { $installFlags += "--fresh-db" }
if ($UseExistingDb) { $installFlags += "--use-existing-db" }
$installFlagStr = ($installFlags -join " ")

Write-Host "==> [1/4] Uploading .env.docker (if provided)..."
if ($EnvFile) {
    if (-not (Test-Path $EnvFile)) { throw "EnvFile not found: $EnvFile" }
    & ssh @sshArgs $sshTarget "mkdir -p '$VmProjectDir'"
    & scp @scpArgs $EnvFile "${sshTarget}:${VmProjectDir}/.env.docker"
    & ssh @sshArgs $sshTarget "sed -i 's/\r$//' '$VmProjectDir/.env.docker'"
    Write-Host "    Uploaded $EnvFile"
} else {
    $localEnv = Join-Path $RepoRoot ".env.docker"
    if (Test-Path $localEnv) {
        & ssh @sshArgs $sshTarget "mkdir -p '$VmProjectDir'"
        & scp @scpArgs $localEnv "${sshTarget}:${VmProjectDir}/.env.docker"
        & ssh @sshArgs $sshTarget "sed -i 's/\r$//' '$VmProjectDir/.env.docker'"
        Write-Host "    Uploaded repo .env.docker"
    } else {
        Write-Host "    No .env.docker to upload - VM must already have one."
    }
}

Write-Host "==> [2/4] Running vm-install-deploy.sh on VM (Docker + app + HTTPS)..."
$remoteCmd = "cd '$VmProjectDir' 2>/dev/null || true; if [ ! -f deploy/vm-install-deploy.sh ]; then git clone -b master https://github.com/tamajit20/options_advisor_system.git '$VmProjectDir' && cd '$VmProjectDir'; fi; chmod +x deploy/vm-install-deploy.sh deploy/azure/open-port-5001.sh deploy/azure/open-port-https.sh deploy/azure/enable-https.sh 2>/dev/null; ./deploy/vm-install-deploy.sh $installFlagStr"
& ssh @sshArgs $sshTarget $remoteCmd

Write-Host "==> [3/4] Ensuring self-signed HTTPS on VM..."
& (Join-Path $DeployDir "enable-https-remote.ps1") -VmHost $VmHost -SkipPortOpen -Mode selfsigned

if (-not $SkipPortOpen) {
    Write-Host "==> [4/4] Tailscale, then close public inbound 22/80/443/5001..."
    & (Join-Path $DeployDir "enable-tailscale.ps1")
    if ($LASTEXITCODE -ne 0) { throw "enable-tailscale.ps1 failed" }
} else {
    Write-Host "==> [4/4] Skipped Tailscale lockdown (-SkipPortOpen)."
}

Write-Host ""
Write-Host "Done. Dashboard is on the Tailscale address printed above."
Write-Host "Public SSH and HTTPS are closed."
Write-Host "Zerodha login (each trading morning, SSH to the Tailscale address in laptop.config.ps1):"
Write-Host "  ssh -i `"$SshKeyPath`" ${VmUser}@<VmHost>"
Write-Host "  cd $VmProjectDir && set -a && source .env.docker && set +a && export COMPOSE_PROFILES=`"`${COMPOSE_PROFILES:-bundled,https}`""
Write-Host "  sg docker -c 'docker compose exec options_advisor python main.py --zerodha-login'"
