#!/usr/bin/env python3
"""Predownload the model checkpoints into the Hugging Face cache.

Covers m-a-p/YuE2-3B, m-a-p/YuE2-Vae, m-a-p/YuE2-Vae-legacy (via the YuE2
runtime), and m-a-p/SheetSage2 plus the pinned MERT-v2-FullSong encoder parent.
Shared by download-models.sh and download-models.ps1. Requires network access.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def yue2_checkpoints() -> None:
    from yue2.storage import resolve_model

    for model in ("m-a-p/YuE2-3B", "m-a-p/YuE2-Vae", "m-a-p/YuE2-Vae-legacy"):
        print(f"downloading {model} ...", flush=True)
        path = resolve_model(model)
        print(f"  ok: {path}", flush=True)


def sheetsage_checkpoint() -> None:
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


def main() -> int:
    print("== YuE2 checkpoints")
    yue2_checkpoints()
    print("== SheetSage2 + MERT-v2-FullSong encoder")
    sheetsage_checkpoint()
    print("All checkpoints cached.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
