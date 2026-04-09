# LMCP Launcher
# Opens the HTTP surface. UI available at http://127.0.0.1:7345/ui
#
# Usage:
#   .\start_lmcp.ps1                          # uses config/registry.yaml
#   .\start_lmcp.ps1 -Registry path\to\reg.yaml  # custom registry

param(
    [string]$Registry = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# Find registry: explicit param > config/registry.yaml > error
if ($Registry -eq "") {
    $Registry = Join-Path $scriptDir "config\registry.yaml"
}
if (-not (Test-Path $Registry)) {
    Write-Host "Registry not found: $Registry" -ForegroundColor Red
    Write-Host "Copy config/registry.example.yaml to config/registry.yaml and configure it." -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting LMCP..." -ForegroundColor Cyan
Write-Host "  Registry: $Registry" -ForegroundColor DarkGray
Write-Host "  UI: http://127.0.0.1:7345/ui" -ForegroundColor DarkGray

# Open browser after a short delay (daemon needs a moment to bind)
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 2
    Start-Process "http://127.0.0.1:7345/ui"
} | Out-Null

python -m lmcp --registry $Registry --serve-http
