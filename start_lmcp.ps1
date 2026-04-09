# LMCP Launcher
# Run from the LMCP project root. Opens the HTTP surface with the default registry.
# UI available at http://127.0.0.1:7345/ui

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "Starting LMCP..." -ForegroundColor Cyan
python -m lmcp --serve-http
