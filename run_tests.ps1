# run_tests.ps1 — convenience runner for the unit suite
# Usage:  .\run_tests.ps1            # full run + HTML coverage report
#         .\run_tests.ps1 -Quick     # no coverage, just pass/fail
#         .\run_tests.ps1 -Marker future   # only show deferred placeholders

param(
    [switch]$Quick,
    [string]$Marker = ""
)

$ErrorActionPreference = "Stop"

$markerArg = ""
if ($Marker) { $markerArg = "-m `"$Marker`"" }

if ($Quick) {
    Write-Host "Running tests (no coverage)..." -ForegroundColor Cyan
    if ($markerArg) {
        pytest -v $markerArg
    } else {
        pytest -v
    }
    exit $LASTEXITCODE
}

Write-Host "Running tests with coverage..." -ForegroundColor Cyan
$covArgs = @(
    "--cov=engine",
    "--cov=lifecycle",
    "--cov=database",
    "--cov=downloader",
    "--cov=simulation",
    "--cov=dashboard",
    "--cov-report=term-missing",
    "--cov-report=html:tests/coverage_html"
)

if ($markerArg) {
    pytest -v @covArgs $markerArg
} else {
    pytest -v @covArgs
}

$exitCode = $LASTEXITCODE

if (Test-Path "tests/coverage_html/index.html") {
    Write-Host ""
    Write-Host "HTML coverage report: tests/coverage_html/index.html" -ForegroundColor Green
}

exit $exitCode
