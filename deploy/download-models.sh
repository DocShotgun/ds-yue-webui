#!/usr/bin/env bash
#
# Predownload model checkpoints into the Hugging Face cache so the first job
# does not have to wait for a multi-GB download:
#   m-a-p/YuE2-3B, m-a-p/YuE2-Vae, m-a-p/YuE2-Vae-legacy (via the YuE2 runtime),
#   m-a-p/SheetSage2 + the pinned MERT-v2-FullSong encoder parent.
#
# Run after install.sh (uses the venv's python). Requires network access.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${YUE_WEBUI_VENV:-$ROOT/.venv}"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "venv not found at $VENV; run deploy/install.sh first (or set YUE_WEBUI_VENV)" >&2
  exit 1
fi

echo "== YuE2 checkpoints"
"$PY" - <<'PY'
from yue2.storage import resolve_model

for model in ("m-a-p/YuE2-3B", "m-a-p/YuE2-Vae", "m-a-p/YuE2-Vae-legacy"):
    print(f"downloading {model} ...", flush=True)
    path = resolve_model(model)
    print(f"  ok: {path}", flush=True)
PY

echo "== SheetSage2 + MERT-v2-FullSong encoder"
"$PY" - <<'PY'
import json
from pathlib import Path

from huggingface_hub import snapshot_download

path = snapshot_download("m-a-p/SheetSage2")
print(f"  ok: {path}", flush=True)
config = json.loads((Path(path) / "config.json").read_text())
base = config.get("base_model_name_or_path") or config.get("_name_or_path")
revision = config.get("base_model_revision") or config.get("_commit_hash")
if base:
    print(f"downloading encoder parent {base} (revision {revision}) ...", flush=True)
    snapshot_download(base, revision=revision)
    print("  ok", flush=True)
PY

echo "All checkpoints cached. Start the server with: bash deploy/run.sh"
