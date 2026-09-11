# Start the ds-yue-webui server on Windows (mirrors deploy/run.sh).
#
# Run from cmd via the .bat wrapper:
#   deploy\run.bat
#
# Host/port/residency come from config.yaml; override with flags:
#   deploy\run.bat --port 9000 --host 0.0.0.0
# or via env: YUE_WEBUI_CONFIG=path\to\config.yaml

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

$Config = $env:YUE_WEBUI_CONFIG
if (-not $Config) { $Config = Join-Path $Root "config.yaml" }
if (Test-Path $Config) {
  Write-Output "config: $Config"
} else {
  Write-Warning "no config.yaml at $Config; using built-in defaults (data dir: $(Join-Path $Root 'data'))"
}

# pass through extra args (--port, --host, ...) to the server
$serverArgs = @("--config", $Config)
if ($args) { $serverArgs += $args }
& $Py -m server @serverArgs
