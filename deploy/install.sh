#!/usr/bin/env bash
#
# ds-yue-webui installer (Linux, target ML server).
#
# Creates the uv-managed venv(s), installs the YuE2 runtime (from GitHub; a
# local checkout is optional), writes config.yaml (only if missing), and
# optionally runs the opt-in smoke test. Run from anywhere; paths are resolved
# relative to the project root (the parent of deploy/). No other repository
# checkouts are required before setup.
#
# Flags:
#   --strict-pins      Two-venv fallback exactly per the upstream READMEs:
#                      .venv (server + YuE2 with its pinned deps) and
#                      .venv-sheetsage2 (torch 2.8 / transformers 4.45.2 stack).
#   --latest-torch     Float torch beyond the upstream pin (torchaudio 2.11+
#                      follows without pinning torch).
#   --yue-source SRC   YuE2 runtime source: "fa-fix" (DocShotgun/YuE @
#                      flash-attention-fix) or "upstream". Default: upstream
#                      (fa-fix is the install.ps1 default on Windows; force it
#                      here for Git Bash installs). A --yue-rev pin implies
#                      upstream.
#   --vendored-mel     Apply the vendored mel-frontend patch (only needed when
#                      torchaudio cannot be installed for your torch build).
#   --smoke            Opt-in smoke test after installation (small real YuE2
#                      generate + SheetSage2 transcribe; writes data/smoke-result.json
#                      and shows on the server's Diagnostics page).
#   --data-dir DIR     Data directory (default: data). Written to config.yaml.
#   --venv-dir DIR     Venv directory (default: .venv).
#   --port INT         Port for config.yaml (default: 8765).
#   --yue-dir DIR      OPT-IN: install the YuE2 runtime from a LOCAL checkout
#                      instead of GitHub, and write yue2.dir into config.yaml
#                      (only if you have local modifications).
#   --yue-rev REV      Pin the YuE2 git install (tag/commit; default branch).
#   --sheetsage-dir DIR  OPT-IN: vendored-mel code source: local SheetSage2
#                      checkout, and writes sheetsage2.dir into config.yaml
#                      (the Hub is used otherwise).
#   --revision REV     Pin the weight downloads (SheetSage2 patch path) to a revision.
#   --skip-models      Skip the model predownload reminder at the end.
#
# Re-run safely: installs are idempotent and config.yaml is never clobbered
# once it exists (the smoke test and patch script update specific keys only).
# No other repository checkouts are required: the YuE2 runtime installs from
# GitHub, abc_tools is vendored inside this repo, and local checkouts are
# strictly opt-in via --yue-dir / --sheetsage-dir.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STRICT_PINS=0; SMOKE=0; LATEST_TORCH=0; VENDORED_MEL=0
YUE_SOURCE_ARG=""
DATA_DIR_ARG=""; VENV_DIR=""; PORT_ARG=""; YUE_DIR_ARG=""; YUE_REV_ARG=""; SHEETSAGE_DIR_ARG=""; REVISION_ARG=""; SKIP_MODELS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --strict-pins) STRICT_PINS=1 ;;
    --smoke) SMOKE=1 ;;
    --latest-torch) LATEST_TORCH=1 ;;
    --vendored-mel) VENDORED_MEL=1 ;;
    --yue-source) YUE_SOURCE_ARG="$2"; shift ;;
    --data-dir) DATA_DIR_ARG="$2"; shift ;;
    --venv-dir) VENV_DIR="$2"; shift ;;
    --port) PORT_ARG="$2"; shift ;;
    --yue-dir) YUE_DIR_ARG="$2"; shift ;;
    --yue-rev) YUE_REV_ARG="$2"; shift ;;
    --sheetsage-dir) SHEETSAGE_DIR_ARG="$2"; shift ;;
    --revision) REVISION_ARG="$2"; shift ;;
    --skip-models) SKIP_MODELS=1 ;;
    -h|--help) sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# YuE runtime source (tri-state: see the header; a --yue-rev pin implies upstream)
case "${YUE_SOURCE_ARG:-}" in
  upstream) YUE_SOURCE="upstream" ;;
  fa-fix|flash-attention-fix) YUE_SOURCE="fa-fix" ;;
  "") YUE_SOURCE="upstream" ;;
  *)
    echo "--yue-source must be upstream or fa-fix, got: $YUE_SOURCE_ARG" >&2
    exit 2 ;;
esac
YUE_DIR="$YUE_DIR_ARG"
if [ -n "$YUE_DIR_ARG" ] && [ ! -f "$YUE_DIR/pyproject.toml" ]; then
  echo "--yue-dir passed but $YUE_DIR does not look like the YuE repo" >&2
  exit 1
fi
SHEETSAGE_DIR="$SHEETSAGE_DIR_ARG"
VENV="$ROOT/${VENV_DIR:-.venv}"
DATA_DIR="${DATA_DIR_ARG:-$ROOT/data}"
PORT="${PORT_ARG:-8765}"

say()  { printf '\n== %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

# ---- checks ----------------------------------------------------------------
say "Checks"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: install it with 'curl -LsSf https://astral.sh/uv/install.sh | sh'" >&2
  exit 1
fi
uv --version
if command -v ffmpeg >/dev/null 2>&1; then ffmpeg -version 2>/dev/null | head -1; else
  warn "ffmpeg not found on PATH; transcription (default preset) and mp3 delivery need it"; fi
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true; else
  warn "nvidia-smi not found; the YuE2 pipeline needs a BF16-capable NVIDIA GPU"; fi
if [ -n "$YUE_DIR" ]; then
  say "YuE source: local checkout $YUE_DIR (opt-in via --yue-dir; refreshing vendored abc_tools)"
  mkdir -p "$ROOT/vendor/yue2"
  cp "$YUE_DIR/skills/yue2-music/scripts/abc_tools.py" "$ROOT/vendor/yue2/abc_tools.py"
else
  say "YuE source: GitHub (no local checkout needed; pin with --yue-rev)"
fi
if [ -n "$SHEETSAGE_DIR" ]; then
  say "SheetSage2 code source: local checkout $SHEETSAGE_DIR"
else
  warn "No SheetSage2 checkout passed; --vendored-mel fetches code+weights from the HF Hub"
  warn "(opt in with --sheetsage-dir if you have a local checkout)"
fi

# ---- venvs -----------------------------------------------------------------
install_server_venv() {
  say "Creating server venv: $VENV (YuE2 runtime + webui deps)"
  uv venv --allow-existing "$VENV"
  PY="$VENV/bin/python"
  if [ -f "$YUE_DIR/pyproject.toml" ]; then
    uv pip install --python "$PY" "$YUE_DIR"
  else
    case "$YUE_SOURCE" in
      fa-fix)
        say "YuE source: github.com/DocShotgun/YuE @ flash-attention-fix"
        say "(default on Windows: upstream's FA availability check misfires with the Windows"
        say "torch wheels; revisit upstream occasionally. Override with --yue-source upstream)"
        YUE_SRC="git+https://github.com/DocShotgun/YuE@flash-attention-fix"
        if [ -n "$YUE_REV_ARG" ]; then YUE_SRC="$YUE_SRC@$YUE_REV_ARG"; fi
        uv pip install --python "$PY" "$YUE_SRC"
        ;;
      *)
        YUE_SRC="git+https://github.com/multimodal-art-projection/YuE"
        if [ -n "$YUE_REV_ARG" ]; then YUE_SRC="$YUE_SRC@$YUE_REV_ARG"; fi
        uv pip install --python "$PY" "$YUE_SRC"
        ;;
    esac
  fi
  uv pip install --python "$PY" fastapi "uvicorn" python-multipart pyyaml "mir_eval>=0.8.2" pretty_midi mido
  if [ "$LATEST_TORCH" = 1 ]; then
    say "Floating torch to the latest release (torchaudio follows without pinning torch)"
    uv pip install --python "$PY" --upgrade torch torchaudio
  else
    TORCH_VERSION=$("$PY" -c 'import torch; print(torch.__version__.split("+")[0])' 2>/dev/null || true)
    if [ -n "$TORCH_VERSION" ]; then
      say "Installing matching torchaudio $TORCH_VERSION for the pinned torch"
      if ! uv pip install --python "$PY" "torchaudio==$TORCH_VERSION"; then
        warn "torchaudio==$TORCH_VERSION is not available for your platform/index."
        warn "Re-run with --vendored-mel to patch SheetSage2's mel frontend instead of torchaudio."
      fi
    else
      warn "Could not determine the installed torch version; install a matching torchaudio manually."
    fi
  fi
}

install_sheetsage_venv() {
  say "Creating SheetSage2 venv: $ROOT/.venv-sheetsage2 (upstream pinned stack)"
  uv venv --allow-existing "$ROOT/.venv-sheetsage2"
  PY_SHEETSAGE="$ROOT/.venv-sheetsage2/bin/python"
  uv pip install --python "$PY_SHEETSAGE" -r "$SHEETSAGE_DIR/requirements.txt"
}

install_server_venv
if [ "$STRICT_PINS" = 1 ]; then
  if [ -n "$SHEETSAGE_DIR" ]; then
    install_sheetsage_venv
  else
    warn "--strict-pins: no SheetSage2 checkout passed; the pinned stack's requirements live"
    warn "in the checkout, so pass --sheetsage-dir to install the strict venv. Continuing."
  fi
else
  PY_SHEETSAGE="$VENV/bin/python"
fi

# ---- torchaudio / mel-frontend patch ----------------------------------------
if [ "$VENDORED_MEL" = 1 ]; then
  say "Applying the vendored mel-frontend patch"
  MEL_ARGS=(--data-dir "$DATA_DIR" --config "$ROOT/config.yaml" ${REVISION_ARG:+--revision "$REVISION_ARG"})
  if [ -f "$SHEETSAGE_DIR/modeling_mert2.py" ]; then
    MEL_ARGS+=(--sheetsage-dir "$SHEETSAGE_DIR")
  fi
  "$PY" deploy/apply_mel_patch.py "${MEL_ARGS[@]}"
  PY_SHEETSAGE="$VENV/bin/python"
  if [ "$STRICT_PINS" = 1 ]; then
    warn "--vendored-mel with --strict-pins: the patched model runs inside the shared venv; the"
    warn "strict SheetSage2 venv (.venv-sheetsage2) is installed but unused unless you point"
    warn "worker.python_sheetsage2 (config.yaml) back at it and keep torchaudio installed there."
    PY_SHEETSAGE="$ROOT/.venv-sheetsage2/bin/python"
  fi
else
  if "$VENV/bin/python" -c "import torchaudio" >/dev/null 2>&1; then
    say "torchaudio available: $(cd "$ROOT" && "$VENV/bin/python" -c 'import torchaudio; print(torchaudio.__version__)')"
  else
    warn "torchaudio is not importable in the venv. SheetSage2's mel frontend needs torchaudio."
    warn "Fix by re-running with --vendored-mel (vendors the three mel transforms), or install a"
    warn "compatible torchaudio: 'uv pip install --python $VENV/bin/python torchaudio'."
  fi
fi

# ---- config.yaml -------------------------------------------------------------
say "Writing config.yaml (only if missing)"
STRICT_FLAG=""
if [ "$STRICT_PINS" = 1 ]; then STRICT_FLAG="--strict-pins"; fi
"$PY" deploy/write_config.py "$ROOT" "$DATA_DIR" "$PORT" "$YUE_DIR" "$SHEETSAGE_DIR" $STRICT_FLAG

# ---- smoke test (opt-in) ------------------------------------------------------
if [ "$SMOKE" = 1 ]; then
  say "Smoke test (opt-in): small YuE2 generate + SheetSage2 transcribe"
  "$PY" deploy/write_smoke_specs.py "$DATA_DIR"
  "$VENV/bin/python" worker/yue2_worker.py smoke --spec "$DATA_DIR/smoke/yue2-spec.json"
  "$VENV/bin/python" worker/sheetsage_worker.py smoke --spec "$DATA_DIR/smoke/sheetsage-spec.json"
  "$PY" deploy/collect_smoke.py "$DATA_DIR"
else
  say "Smoke test skipped (opt in with --smoke)"
fi

# ---- model predownload reminder -------------------------------------------------
if [ "$SKIP_MODELS" = 0 ]; then
  say "Next: predownload model checkpoints (or let the first job download them)"
  echo "  bash deploy/download-models.sh"
fi

say "Start the server"
echo "  bash deploy/run.sh            # tmux/foreground; binds per config.yaml (host/port)"
echo "  tmux new -s yue-webui 'bash deploy/run.sh'"
echo ""
echo "Then open http://<server-ip>:$PORT/ (LAN or Tailscale). Diagnostics page shows"
echo "env health, the smoke result (if run), GPU status, and config; residency can be"
echo "toggled there. If the shared env misbehaves: re-run install.sh --strict-pins."
