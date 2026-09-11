# Predownload model checkpoints into the Hugging Face cache on Windows
# (mirrors deploy/download-models.sh). Run after deploy\install.bat.

$ErrorActionPreference = "Stop"

$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

if ($null -ne $IsWindows) { $IsWin = $IsWindows } else { $IsWin = ($env:OS -eq "Windows_NT") }
$PyJoin = if ($IsWin) { "Scripts\python.exe" } else { "bin/python" }

$Venv = $env:YUE_WEBUI_VENV
if (-not $Venv) { $Venv = Join-Path $Root ".venv" }
$Py = Join-Path $Venv $PyJoin

if (-not (Test-Path $Py)) {
  Write-Error "venv not found at $Venv; run deploy\install.bat first (or set YUE_WEBUI_VENV)"
  exit 1
}

& $Py deploy/predownload.py
Write-Output "Start the server with: deploy\run.bat"
