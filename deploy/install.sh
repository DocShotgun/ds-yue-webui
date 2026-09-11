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
#   --vendored-mel     Apply the vendored mel-frontend patch (only needed when
#                      torchaudio cannot be installed for your torch build).
#   --smoke            Opt-in smoke test after installation (small real YuE2
#                      generate + SheetSage2 transcribe; writes data/smoke-result.json
#                      and shows on the server's Diagnostics page).
#   --data-dir DIR     Data directory (default: data). Written to config.yaml.
#   --venv-dir DIR     Venv directory (default: .venv).
#   --port INT         Port for config.yaml (default: 8765).
#   --yue-dir DIR      Install the YuE2 runtime from a LOCAL checkout instead of
#                      GitHub (only needed if you have local modifications).
#   --yue-rev REV      Pin the YuE2 git install (tag/commit; default branch).
#   --sheetsage-dir DIR  Vendored-mel code source: local SheetSage2 checkout
#                      (optional even for --vendored-mel: the Hub is used otherwise).
#   --revision REV     Pin the weight downloads (SheetSage2 patch path) to a revision.
#   --skip-models      Skip the model predownload reminder at the end.
#
# Re-run safely: installs are idempotent and config.yaml is never clobbered
# once it exists (the smoke test and patch script update specific keys only).
# No other repository checkouts are required: the YuE2 runtime installs from
# GitHub and abc_tools is vendored inside this repo.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STRICT_PINS=0; SMOKE=0; LATEST_TORCH=0; VENDORED_MEL=0
DATA_DIR_ARG=""; VENV_DIR=""; PORT_ARG=""; YUE_DIR_ARG=""; YUE_REV_ARG=""; SHEETSAGE_DIR_ARG=""; REVISION_ARG=""; SKIP_MODELS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --strict-pins) STRICT_PINS=1 ;;
    --smoke) SMOKE=1 ;;
    --latest-torch) LATEST_TORCH=1 ;;
    --vendored-mel) VENDORED_MEL=1 ;;
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

YUE_DIR="${YUE_DIR_ARG:-$ROOT/../YuE}"
if [ -n "$YUE_DIR_ARG" ] && [ ! -f "$YUE_DIR/pyproject.toml" ]; then
  echo "--yue-dir passed but $YUE_DIR does not look like the YuE repo" >&2
  exit 1
fi
SHEETSAGE_DIR="${SHEETSAGE_DIR_ARG:-$ROOT/../SheetSage2}"
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
if [ -f "$YUE_DIR/pyproject.toml" ]; then
  say "YuE source: local checkout $YUE_DIR (refreshing vendored abc_tools)"
  mkdir -p "$ROOT/vendor/yue2"
  cp "$YUE_DIR/skills/yue2-music/scripts/abc_tools.py" "$ROOT/vendor/yue2/abc_tools.py"
else
  say "YuE source: GitHub (no local checkout needed; pin with --yue-rev)"
fi
if [ ! -f "$SHEETSAGE_DIR/modeling_mert2.py" ]; then
  if [ "$VENDORED_MEL" = 1 ]; then
    if [ -n "$SHEETSAGE_DIR_ARG" ]; then echo "$SHEETSAGE_DIR does not look like the SheetSage2 repo" >&2; exit 1; fi
    warn "SheetSage2 checkout not found; --vendored-mel will fetch code+weights from the HF Hub"
  else
    warn "SheetSage2 checkout not found; continuing (it is only the code source for --vendored-mel; weights resolve from the HF Hub)"
  fi
fi

# ---- venvs -----------------------------------------------------------------
install_server_venv() {
  say "Creating server venv: $VENV (YuE2 runtime + webui deps)"
  uv venv "$VENV"
  PY="$VENV/bin/python"
  if [ -f "$YUE_DIR/pyproject.toml" ]; then
    uv pip install --python "$PY" "$YUE_DIR"
  else
    YUE_SRC="git+https://github.com/multimodal-art-projection/YuE"
    if [ -n "$YUE_REV_ARG" ]; then YUE_SRC="$YUE_SRC@$YUE_REV_ARG"; fi
    uv pip install --python "$PY" "$YUE_SRC"
  fi
  uv pip install --python "$PY" fastapi "uvicorn" python-multipart pyyaml "mir_eval>=0.8.2.post1" pretty_midi mido
  if [ "$LATEST_TORCH" = 1 ]; then
    say "Floating torch to the latest release (torchaudio follows without pinning torch)"
    uv pip install --python "$PY" --upgrade torch torchaudio
  fi
}

install_sheetsage_venv() {
  say "Creating SheetSage2 venv: $ROOT/.venv-sheetsage2 (upstream pinned stack)"
  uv venv "$ROOT/.venv-sheetsage2"
  PY_SHEETSAGE="$ROOT/.venv-sheetsage2/bin/python"
  uv pip install --python "$PY_SHEETSAGE" -r "$SHEETSAGE_DIR/requirements.txt"
}

install_server_venv
if [ "$STRICT_PINS" = 1 ]; then
  install_sheetsage_venv
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
"$PY" - "$ROOT" "$DATA_DIR" "$PORT" "$YUE_DIR" "$SHEETSAGE_DIR" "$STRICT_PINS" <<'PY'
import sys
from pathlib import Path
import yaml

root, data_dir, port, yue_dir, sheetsage_dir, strict = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5], sys.argv[6] == "1"
config_path = Path(root) / "config.yaml"
raw = {}
if config_path.is_file():
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw = loaded if isinstance(loaded, dict) else {}
    print(f"config.yaml already exists; leaving values as-is ({config_path})")
else:
    raw = {
        "host": "0.0.0.0",
        "port": port,
        "data_dir": data_dir,
        "residency": "on-demand",       # on-demand | always
        "release_idle_minutes": 10,
        "offline": False,
        "memory_budget_gib": 24.0,
        "yue2": {
            "model": "m-a-p/YuE2-3B",
            "vae": "m-a-p/YuE2-Vae",
            "vae_legacy": "m-a-p/YuE2-Vae-legacy",
            "device": "auto",           # auto | cuda | cpu
            "backend": "torch",         # torch | torch-eager | vllm
            "quantization": "none",     # none | fp8
            "offload_ar": False,
        },
        "sheetsage2": {
            "model": "auto",            # auto: local dir with weights, else m-a-p/SheetSage2
            "device": "cuda",
            "dtype": "bf16",
        },
        "worker": {
            "python": "auto",           # auto -> the venv the server runs in
            "python_yue2": "auto",      # auto -> worker.python
            "python_sheetsage2": "auto",
        },
    }
    print(f"config.yaml written ({config_path})")
    # dir keys are only relevant when a local checkout is actually used;
    # omit them so the config reflects a checkout-free setup by default
    if Path(yue_dir, "pyproject.toml").is_file():
        raw["yue2"]["dir"] = yue_dir
    if Path(sheetsage_dir, "modeling_mert2.py").is_file():
        raw["sheetsage2"]["dir"] = sheetsage_dir
if strict:
    raw.setdefault("worker", {})
    raw["worker"]["python_sheetsage2"] = str(Path(root) / ".venv-sheetsage2" / "bin" / "python")
    print("strict-pins: worker.python_sheetsage2 -> .venv-sheetsage2/bin/python")
config_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY

# ---- smoke test (opt-in) ------------------------------------------------------
if [ "$SMOKE" = 1 ]; then
  say "Smoke test (opt-in): small YuE2 generate + SheetSage2 transcribe"
  mkdir -p "$DATA_DIR/smoke"
  "$PY" - "$ROOT" "$DATA_DIR" "$VENV/bin/python" "$YUE_DIR" "$SHEETSAGE_DIR" <<'PY'
import json
import sys
from pathlib import Path

root, data_dir, python, yue_dir, sheetsage_dir = sys.argv[1:]
data_dir = Path(data_dir)
request = {
    "id": "smoke_test",
    "style": "English, warm piano, acoustic pop, female vocal, 88 BPM",
    "lyrics": "[Verse]\nSmoke test Signal checks the sound\nShort and quiet, safe and found\n[Chorus]\nLet the test resolve to pass\nJust a moment, quiet glass",
    "cot": "full",
    "seed": 831001,
}
# Token-capped sampling keeps the smoke generate short (~1-2 minutes on 24GB).
yue2_spec = {
    "model": "m-a-p/YuE2-3B", "vae": "m-a-p/YuE2-Vae", "device": "auto",
    "budget": 24.0, "backend": "torch", "quantization": "none", "offload_ar": False,
    "offline": False,
    "output_dir": str(data_dir / "smoke" / "yue2"),
    "request": request,
    "sampling": {"abc": {"max_tokens": 64, "min_tokens": 32}, "semantic": {"max_tokens": 128, "min_tokens": 32}},
}
sheetsage_spec = {
    "model": "auto",
    "offline": False, "device": "cuda", "dtype": "bf16",
    "audio": str(data_dir / "smoke" / "yue2" / "audio.flac"),
    "task": "full", "preset": "default", "max_seconds": 30,
    "output_dir": str(data_dir / "smoke" / "sheetsage"),
}
(data_dir / "smoke" / "yue2-spec.json").write_text(json.dumps(yue2_spec, indent=2), encoding="utf-8")
(data_dir / "smoke" / "sheetsage-spec.json").write_text(json.dumps(sheetsage_spec, indent=2), encoding="utf-8")
print("smoke specs written")
PY
  "$VENV/bin/python" worker/yue2_worker.py smoke --spec "$DATA_DIR/smoke/yue2-spec.json"
  "$VENV/bin/python" worker/sheetsage_worker.py smoke --spec "$DATA_DIR/smoke/sheetsage-spec.json"
  "$PY" - "$DATA_DIR" <<'PY'
import json, sys
from pathlib import Path
data_dir = Path(sys.argv[1])
def read(path):
    p = data_dir / path
    if not p.is_file():
        return {"missing": str(p)}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {"unavailable": "not valid JSON"}
yue2 = read("smoke/yue2/result.json")
sheetsage = read("smoke/sheetsage/smoke_status.json")
passed = yue2.get("status") == "complete" and sheetsage.get("status") == "complete"
report = {"passed": passed, "yue2": yue2, "sheetsage": sheetsage,
          "note": "Smoke timing is a rough measure, not a benchmark. "
                  "If the relaxed shared env failed, re-run install.sh --strict-pins."}
(data_dir / "smoke-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(f"smoke {'PASSED' if passed else 'FAILED'}; report: {data_dir / 'smoke-result.json'}")
PY
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
