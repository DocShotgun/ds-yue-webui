#!/usr/bin/env bash
#
# Start the ds-yue-webui server (foreground; wrap in tmux for persistence):
#   tmux new -s yue-webui 'bash deploy/run.sh'
#
# Host/port/residency come from config.yaml; override with flags:
#   bash deploy/run.sh --port 9000 --host 0.0.0.0
# or via env: YUE_WEBUI_CONFIG=/path/config.yaml

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${YUE_WEBUI_VENV:-$ROOT/.venv}"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "venv not found at $VENV; run deploy/install.sh first (or set YUE_WEBUI_VENV)" >&2
  exit 1
fi

CONFIG="${YUE_WEBUI_CONFIG:-$ROOT/config.yaml}"
if [ -f "$CONFIG" ]; then
  echo "config: $CONFIG"
else
  echo "no config.yaml at $CONFIG; using built-in defaults (data dir: $ROOT/data)" >&2
fi

exec "$PY" -m server --config "$CONFIG" "$@"
