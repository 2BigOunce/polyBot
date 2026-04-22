# Deploy V2 migration files to the VPS (PowerShell port of deploy_v2_vps.sh).
# Uses Windows built-in ssh.exe / scp.exe (OpenSSH client, shipped with Win10+).
#
# Usage:
#   $env:VPS_HOST = "ubuntu@3.255.105.112"
#   $env:SSH_KEY  = ".\polybot-dublin.pem"
#   .\scripts\deploy_v2_vps.ps1
#   .\scripts\deploy_v2_vps.ps1 -SkipTest

param(
    [switch]$SkipTest
)

$ErrorActionPreference = "Stop"

if (-not $env:VPS_HOST) { Write-Error "Set `$env:VPS_HOST = 'ubuntu@<ip>'"; exit 1 }
if (-not $env:SSH_KEY)  { Write-Error "Set `$env:SSH_KEY = 'path\to\polybot-dublin.pem'"; exit 1 }

$VpsHost    = $env:VPS_HOST
$SshKey     = $env:SSH_KEY
$RemoteDir  = "/home/ubuntu/polyBot"
$RemoteVenv = "/home/ubuntu/polyBot/venv"
$SshArgs    = @("-i", $SshKey, "-o", "StrictHostKeyChecking=accept-new")

$V2TradingFiles = @(
    "src/trading/arb_executor_v2.py",
    "src/trading/clob_mispricing_executor_v2.py",
    "src/trading/maker_positioning_v2.py",
    "src/trading/real_trader_v2.py"
)
$V2Scripts = @(
    "scripts/wrap_usdc_to_pusd.py",
    "scripts/cancel_all_maker_orders.py",
    "scripts/test_py_clob_v2.py"
)

Write-Host "=== [1/4] Verifying local V2 files exist ==="
foreach ($f in ($V2TradingFiles + $V2Scripts)) {
    if (-not (Test-Path $f)) { Write-Error "missing: $f"; exit 1 }
}
Write-Host "OK all $($V2TradingFiles.Count + $V2Scripts.Count) files present locally"

Write-Host ""
Write-Host "=== [2/4] scp V2 files directly to $VpsHost`:$RemoteDir ==="
& scp @SshArgs $V2TradingFiles "${VpsHost}:${RemoteDir}/src/trading/"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& scp @SshArgs $V2Scripts "${VpsHost}:${RemoteDir}/scripts/"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "OK files copied"

Write-Host ""
Write-Host "=== [3/4] Installing py_clob_client_v2 in VPS venv ==="
$installCmd = @"
set -euo pipefail
cd $RemoteDir
echo 'Using venv: $RemoteVenv'
$RemoteVenv/bin/pip install --upgrade py_clob_client_v2 2>&1 | tail -5
$RemoteVenv/bin/python -c 'import py_clob_client_v2'
echo 'Import OK'
"@
& ssh @SshArgs $VpsHost $installCmd
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "OK SDK installed"

if ($SkipTest) {
    Write-Host ""
    Write-Host "=== [4/4] Skipped smoke test (-SkipTest) ==="
    exit 0
}

Write-Host ""
Write-Host "=== [4/4] Running V2 SDK smoke test on VPS ==="
$testCmd = @"
set -euo pipefail
cd $RemoteDir
PYTHONPATH=. $RemoteVenv/bin/python scripts/test_py_clob_v2.py
"@
& ssh @SshArgs $VpsHost $testCmd
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "OK V2 deployment complete. polybot.service still runs V1 - untouched."
Write-Host "   Next: run scripts/cutover_v2.sh at ~10:45 UTC on 28 April."
