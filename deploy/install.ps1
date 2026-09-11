# ds-yue-webui installer for Windows (mirrors deploy/install.sh).
#
# Run from cmd via the .bat wrapper (or powershell directly):
#   deploy\install.bat                     (default: pinned torch 2.10.0 + CUDA wheels)
#   deploy\install.bat --smoke             (also runs the smoke test)
#   deploy\install.bat --yue-rev REV       (pin the YuE2 runtime to a different revision)
#   deploy\install.bat --latest-torch      (latest torch/torchaudio; cu130 index by default)
#
# Windows caveats handled here:
#   * PyPI's Windows torch wheels are CPU-only when no index URL is given, so the
#     installer preinstalls torch + torchaudio from the PyTorch CUDA index
#     (https://download.pytorch.org/whl/cu128, the default CUDA for torch 2.10.0)
#     BEFORE installing the YuE2 runtime, so its torch dependency resolves
#     against the CUDA wheel instead of the CPU-only PyPI wheel.
#   * Upstream YuE's internal flash-attention availability check misfires with
#     the Windows torch wheels, so by default the runtime installs from the
#     flash-attention-fix branch of DocShotgun/YuE. Override with
#     --yue-source upstream (or a local checkout with --yue-dir DIR).
#     Revisit upstream occasionally: this branch is a temporary workaround.
#
# Flags:
#   --strict-pins        Two-venv fallback per the upstream READMEs: .venv
#                        (server + YuE2) and .venv-sheetsage2 (torch 2.8 stack).
#   --latest-torch       Latest torch/torchaudio from the cu130 index (Windows).
#   --torch-index URL    Override the PyTorch CUDA index URL in use.
#   --yue-source SRC     "fa-fix" (DocShotgun/YuE @ flash-attention-fix), or
#                        "upstream" (default: fa-fix on Windows, upstream
#                        elsewhere). A --yue-rev pin implies upstream.
#   --vendored-mel       Apply the vendored mel-frontend patch (needed when
#                        torchaudio cannot be installed for your torch build).
#   --smoke              Opt-in smoke test after installation.
#   --data-dir DIR       Data directory (default: data). Written to config.yaml.
#   --venv-dir DIR       Venv directory (default: .venv).
#   --port INT           Port for config.yaml (default: 8765).
#   --yue-dir DIR        OPT-IN: install the YuE2 runtime from a LOCAL checkout
#                        and write yue2.dir into config.yaml.
#   --yue-rev REV        Pin the upstream YuE2 git install.
#   --sheetsage-dir DIR  OPT-IN: vendored-mel code source: local SheetSage2
#                        checkout; writes sheetsage2.dir into config.yaml.
#   --revision REV       Pin the weight downloads (SheetSage2 patch path).
#   --skip-models        Skip the model predownload reminder at the end.

$ErrorActionPreference = "Stop"

$ROOT = Split-Path $PSScriptRoot -Parent
Set-Location $ROOT

# ---- parse flags (manual, so --flag and -flag both work from cmd) ------------
$StrictPins = $false; $Smoke = $false; $LatestTorch = $false; $VendoredMel = $false
$YueSourceArg = ""; $SkipModels = $false
$DataDirArg = ""; $VenvDir = ""; $PortArg = 0; $YueDirArg = ""; $YueRevArg = ""
$SheetsageDirArg = ""; $RevisionArg = ""; $TorchIndexArg = ""

function Show-Help {
  Get-Content $PSCommandPath | Where-Object { $_.StartsWith("#") } | ForEach-Object { $_.TrimStart("#").TrimStart() }
}
if ($args) {
  $i = 0
  while ($i -lt $args.Count) {
    switch ($args[$i]) {
      "--strict-pins" { $StrictPins = $true }
      "--smoke" { $Smoke = $true }
      "--latest-torch" { $LatestTorch = $true }
      "--vendored-mel" { $VendoredMel = $true }
      "--yue-source" { $i++; $YueSourceArg = $args[$i] }
      "--skip-models" { $SkipModels = $true }
      "-h" { Show-Help; exit 0 }
      "--help" { Show-Help; exit 0 }
      "--data-dir" { $i++; $DataDirArg = $args[$i] }
      "--venv-dir" { $i++; $VenvDir = $args[$i] }
      "--port" { $i++; $PortArg = [int]$args[$i] }
      "--yue-dir" { $i++; $YueDirArg = $args[$i] }
      "--yue-rev" { $i++; $YueRevArg = $args[$i] }
      "--sheetsage-dir" { $i++; $SheetsageDirArg = $args[$i] }
      "--revision" { $i++; $RevisionArg = $args[$i] }
      "--torch-index" { $i++; $TorchIndexArg = $args[$i] }
      default { Write-Output "unknown option: $($args[$i])"; exit 2 }
    }
    $i++
  }
}

# ---- platform ---------------------------------------------------------------
if ($null -ne $IsWindows) { $IsWin = $IsWindows } else { $IsWin = ($env:OS -eq "Windows_NT") }
$PyJoin = if ($IsWin) { "Scripts\python.exe" } else { "bin/python" }

# Windows torch handling: PyPI Windows wheels are CPU-only, so the CUDA index is
# mandatory. torch 2.10.0's default CUDA was cu128; newer builds use cu130.
$TorchPinVersion = "2.10.0"
$TorchIndexPin = "https://download.pytorch.org/whl/cu128"
$TorchIndexLatest = "https://download.pytorch.org/whl/cu130"
if ($TorchIndexArg) {
  $TorchIndexPin = $TorchIndexArg
  $TorchIndexLatest = $TorchIndexArg
}

# YuE runtime source, one tri-state: "fa-fix" (DocShotgun/YuE @ flash-attention-fix),
# "upstream" (multimodal-art-projection/YuE), or omitted (default: fa-fix on Windows
# where upstream's FA check misfires with the Windows torch wheels, upstream
# elsewhere). A --yue-rev pin implies upstream when the source is omitted.
if ($YueSourceArg) {
  if ($YueSourceArg -notin @("upstream", "fa-fix", "flash-attention-fix")) {
    Write-Output "--yue-source must be upstream or fa-fix, got: $YueSourceArg"
    exit 2
  }
  if ($YueSourceArg -eq "flash-attention-fix") { $YueSourceArg = "fa-fix" }
  $YueSource = $YueSourceArg
} elseif ($YueRevArg) {
  $YueSource = "upstream"
} elseif ($IsWin) {
  $YueSource = "fa-fix"
} else {
  $YueSource = "upstream"
}

$YueDir = if ($YueDirArg) { $YueDirArg } else { "" }
if ($YueDirArg -and -not (Test-Path (Join-Path $YueDir "pyproject.toml"))) {
  Write-Output "--yue-dir passed but $YueDir does not look like the YuE repo"
  exit 1
}
$SheetsageDir = if ($SheetsageDirArg) { $SheetsageDirArg } else { "" }
$Venv = Join-Path $ROOT ($(if ($VenvDir) { $VenvDir } else { ".venv" }))
$DataDir = if ($DataDirArg) { $DataDirArg } else { Join-Path $ROOT "data" }
$Port = if ($PortArg) { $PortArg } else { 8765 }

function Say($text)  { Write-Output ""; Write-Output "== $text" }
function Warn($text) { Write-Warning $text }

# ---- checks ----------------------------------------------------------------
Say "Checks"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
  Write-Error "uv is required: install it with 'powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"'"
  exit 1
}
& uv --version
$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpeg) { (& ffmpeg -version 2>$null | Select-Object -First 1) | Write-Output }
else { Warn "ffmpeg not found on PATH; transcription (default preset) and mp3 delivery need it" }
$nsmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nsmi) { & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader }
else { Warn "nvidia-smi not found; the YuE2 pipeline needs a BF16-capable NVIDIA GPU" }

if ($YueDir) {
  Say "YuE source: local checkout $YueDir (opt-in via --yue-dir; refreshing vendored abc_tools)"
  if ($IsWin) {
    # a local checkout may lack the flash-attention availability fix
    $branch = ""
    try { $branch = (& git -C $YueDir branch --show-current 2>$null) } catch { }
    if (-not $branch) { try { $branch = (& git -C $YueDir rev-parse --short HEAD 2>$null) } catch { } }
    Warn "Local checkout ($branch) - if it lacks the FA availability fix, confirm before relying on it."
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $ROOT "vendor\yue2") | Out-Null
  Copy-Item (Join-Path $YueDir "skills\yue2-music\scripts\abc_tools.py") (Join-Path $ROOT "vendor\yue2\abc_tools.py") -Force
} else {
  Say "YuE source: GitHub (no local checkout needed; pin with --yue-rev)"
}
if ($SheetsageDir) {
  Say "SheetSage2 code source: local checkout $SheetsageDir"
} else {
  Warn "No SheetSage2 checkout passed; --vendored-mel fetches code+weights from the HF Hub"
  Warn "(opt in with --sheetsage-dir if you have a local checkout)"
}

function Install-ServerVenv {
  Say "Creating server venv: $Venv (YuE2 runtime + webui deps)"
  & uv venv $Venv --allow-existing
  $py = "$Venv\$PyJoin"
  if ($IsWin -and -not $LatestTorch) {
    Say "Windows: installing CUDA torch wheels first (torch $TorchPinVersion from the PyTorch index)"
    Say "index: $TorchIndexPin"
    & uv pip install --python $py "torch==$TorchPinVersion" "torchaudio==$TorchPinVersion" --index-url $TorchIndexPin
    if ($LASTEXITCODE -ne 0) {
      Warn "torch/torchaudio $TorchPinVersion from $TorchIndexPin failed to install."
      Warn "Try --torch-index with a different CUDA index (e.g. cu129/cu130), or --latest-torch."
      exit 1
    }
  }
  if ($YueDirArg) {
    & uv pip install --python $py $YueDir
  } elseif ($YueSource -eq "fa-fix") {
    Say "YuE source: github.com/DocShotgun/YuE @ flash-attention-fix"
    Say "(default on Windows: upstream's FA availability check misfires with the Windows torch wheels;"
    Say " revisit upstream occasionally. Override with --yue-source upstream or --yue-dir)"
    $src = "git+https://github.com/DocShotgun/YuE@flash-attention-fix"
    if ($YueRevArg) { $src = "$src@$YueRevArg" }
    & uv pip install --python $py $src
  } else {
    $src = "git+https://github.com/multimodal-art-projection/YuE"
    if ($YueRevArg) { $src = "$src@$YueRevArg" }
    & uv pip install --python $py $src
  }
  if ($LASTEXITCODE -ne 0) { exit 1 }
  & uv pip install --python $py fastapi uvicorn python-multipart pyyaml "mir_eval>=0.8.2" pretty_midi mido
  if ($LASTEXITCODE -ne 0) { exit 1 }
  if (-not $IsWin) {
    if ($LatestTorch) {
      Say "Floating torch to the latest release (torchaudio follows without pinning torch)"
      & uv pip install --python $py --upgrade torch torchaudio
    } else {
      $torchVersion = $null
      try { $torchVersion = (& $py -c 'import torch; print(torch.__version__.split("+")[0])' 2>$null) } catch { }
      if ($torchVersion) {
        Say "Installing matching torchaudio $torchVersion for the pinned torch"
        & uv pip install --python $py "torchaudio==$torchVersion"
        if ($LASTEXITCODE -ne 0) {
          Warn "torchaudio==$torchVersion is not available for your platform/index."
          Warn "Re-run with --vendored-mel to patch SheetSage2's mel frontend instead of torchaudio."
        }
      } else {
        Warn "Could not determine the installed torch version; install a matching torchaudio manually."
      }
    }
  }
}

function Install-SheetsageVenv {
  Say "Creating SheetSage2 venv: $(Join-Path $ROOT '.venv-sheetsage2') (upstream pinned stack)"
  & uv venv (Join-Path $ROOT ".venv-sheetsage2") --allow-existing
  $pySheetsage = Join-Path (Join-Path $ROOT ".venv-sheetsage2") $PyJoin
  if ($IsWin) {
    # PyPI's Windows torch wheels are CPU-only; let the CUDA index satisfy the
    # torch pin (its wheels carry a +cuNNN local version, so they win override).
    & uv pip install --python $pySheetsage -r (Join-Path $SheetsageDir "requirements.txt") --extra-index-url $TorchIndexPin
  } else {
    & uv pip install --python $pySheetsage -r (Join-Path $SheetsageDir "requirements.txt")
  }
  if ($LASTEXITCODE -ne 0) { exit 1 }
}

Install-ServerVenv
if ($StrictPins) {
  if ($SheetsageDir) { Install-SheetsageVenv }
  else {
    Warn "--strict-pins: no SheetSage2 checkout passed; the pinned stack's requirements live"
    Warn "in the checkout, so pass --sheetsage-dir to install the strict venv. Continuing."
  }
}

# ---- torchaudio / mel-frontend patch ----------------------------------------
$PySheetsage = "$Venv\$PyJoin"
if ($VendoredMel) {
  Say "Applying the vendored mel-frontend patch"
  $melArgs = @("--data-dir", $DataDir, "--config", (Join-Path $ROOT "config.yaml"))
  if ($RevisionArg) { $melArgs += @("--revision", $RevisionArg) }
  if ($SheetsageDir -and (Test-Path (Join-Path $SheetsageDir "modeling_mert2.py"))) { $melArgs += @("--sheetsage-dir", $SheetsageDir) }
  & $Venv\$PyJoin deploy/apply_mel_patch.py @melArgs
  if ($LASTEXITCODE -ne 0) { exit 1 }
  $PySheetsage = "$Venv\$PyJoin"
} else {
  $ImportOk = $false
  try {
    & $Venv\$PyJoin -c "import torchaudio" 2>$null
    $ImportOk = ($LASTEXITCODE -eq 0)
  } catch { $ImportOk = $false }
  if ($ImportOk) {
    $taVersion = & $Venv\$PyJoin -c 'import torchaudio; print(torchaudio.__version__)'
    Say "torchaudio available: $taVersion"
  } else {
    Warn "torchaudio is not importable in the venv. SheetSage2's mel frontend needs torchaudio."
    Warn "Fix by re-running with --vendored-mel (vendors the three mel transforms), or check the torch install."
  }
}

# ---- config.yaml -------------------------------------------------------------
Say "Writing config.yaml (only if missing)"
# Write local checkout dir keys when the args point at real checkouts
# (PowerShell drops empty-string args at the native boundary, so a "." is
# passed for the absent checkout and write_config treats it as no checkout)
$checkout1 = if ($YueDir) { $YueDir } else { "." }
$checkout2 = if ($SheetsageDir) { $SheetsageDir } else { "." }
$configArgs = @($ROOT, $DataDir, [string]$Port, $checkout1, $checkout2)
if ($StrictPins) { $configArgs += "--strict-pins" }
& $Venv\$PyJoin deploy/write_config.py @configArgs
if ($LASTEXITCODE -ne 0) { exit 1 }

# ---- smoke test (opt-in) ------------------------------------------------------
if ($Smoke) {
  Say "Smoke test (opt-in): small YuE2 generate + SheetSage2 transcribe"
  & $Venv\$PyJoin deploy/write_smoke_specs.py $DataDir
  $env:PYTHONUTF8 = "1"   # the runtime writes json with default-encoding calls;
                          # the Windows locale default mangles non-ASCII text
  & $Venv\$PyJoin worker/yue2_worker.py smoke --spec (Join-Path $DataDir "smoke\yue2-spec.json")
  & $Venv\$PyJoin deploy/collect_smoke.py $DataDir
} else {
  Say "Smoke test skipped (opt in with --smoke)"
}

# ---- model predownload reminder -------------------------------------------------
if (-not $SkipModels) {
  Say "Next: predownload model checkpoints (or let the first job download them)"
  Write-Output "  deploy\download-models.bat"
}

Say "Start the server"
Write-Output "  deploy\run.bat                # binds per config.yaml (host/port)"
Write-Output ""
Write-Output "Then open http://<server-ip>:$Port/ (LAN or Tailscale). Diagnostics page shows"
Write-Output "env health, the smoke result (if run), GPU status, and config; residency can be"
Write-Output "toggled there. If the shared env misbehaves: re-run install.bat --strict-pins."
