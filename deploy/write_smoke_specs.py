#!/usr/bin/env python3
"""Write the smoke-test spec files (shared by install.sh and install.ps1).

Usage: write_smoke_specs.py DATA_DIR
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    data_dir = Path(sys.argv[1])
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
        "budget": 24.0, "backend": "torch", "quantization": "none", "offload_ar": True,
        "offline": False,
        "output_dir": str(data_dir / "smoke" / "yue2"),
        "request": request,
        "sampling": {"abc": {"max_tokens": 64, "min_tokens": 32}, "semantic": {"max_tokens": 128, "min_tokens": 32}},
    }
    sheetsage_spec = {
        "model": "m-a-p/SheetSage2",
        "offline": False, "device": "cuda", "dtype": "bf16",
        "audio": str(data_dir / "smoke" / "yue2" / "audio.flac"),
        "task": "full", "preset": "default", "max_seconds": 30,
        "output_dir": str(data_dir / "smoke" / "sheetsage"),
    }
    (data_dir / "smoke").mkdir(parents=True, exist_ok=True)
    (data_dir / "smoke" / "yue2-spec.json").write_text(json.dumps(yue2_spec, indent=2), encoding="utf-8")
    (data_dir / "smoke" / "sheetsage-spec.json").write_text(json.dumps(sheetsage_spec, indent=2), encoding="utf-8")
    print("smoke specs written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
