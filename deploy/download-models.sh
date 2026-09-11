#!/usr/bin/env bash
#
# Predownload model checkpoints into the Hugging Face cache so the first job
# does not have to wait for a multi-GB download:
#   m-a-p/YuE2-3B, m-a-p/YuE2-Vae, m-a-p/YuE2-Vae-legacy (via the YuE2 runtime),
#   m-a-p/SheetSage2 + the pinned MERT-v2-FullSong encoder parent.
#
# Run after install.sh (uses the venv's python). Requires network access.
# Windows: use deploy/download-models.bat.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${YUE_WEBUI_VENV:-$ROOT/.venv}"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "venv not found at $VENV; run deploy/install.sh first (or set YUE_WEBUI_VENV)" >&2
  exit 1
fi

"$PY" deploy/predownload.py

echo "Start the server with: bash deploy/run.sh"
