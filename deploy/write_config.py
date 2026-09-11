#!/usr/bin/env python3
"""Write config.yaml (only if missing). Shared by install.sh and install.ps1.

Usage: write_config.py ROOT DATA_DIR PORT YUE_DIR SHEETSAGE_DIR [--strict-pins]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def strict_python_path(root: str) -> str:
    """The SheetSage2 venv interpreter, per the platform's venv layout."""
    venv = Path(root) / ".venv-sheetsage2"
    name = "python.exe" if sys.platform == "win32" else "python"
    return str(venv / ("Scripts" if sys.platform == "win32" else "bin") / name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root")
    parser.add_argument("data_dir")
    parser.add_argument("port", type=int)
    # optional: local checkouts are opt-in; may be omitted entirely
    parser.add_argument("yue_dir", nargs="?", default="")
    parser.add_argument("sheetsage_dir", nargs="?", default="")
    parser.add_argument("--strict-pins", action="store_true")
    args = parser.parse_args()
    # "." is the install.ps1 placeholder for "no checkout" (empty strings do not
    # survive the PowerShell -> native argument boundary)
    if args.yue_dir == ".":
        args.yue_dir = ""
    if args.sheetsage_dir == ".":
        args.sheetsage_dir = ""

    config_path = Path(args.root) / "config.yaml"
    raw = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        raw = loaded if isinstance(loaded, dict) else {}
        print(f"config.yaml already exists; leaving values as-is ({config_path})")
    else:
        raw = {
            "host": "0.0.0.0",
            "port": args.port,
            "data_dir": args.data_dir,
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
    # dir keys are only relevant when a local checkout is actually used (opt-in
    # via --yue-dir / --sheetsage-dir); omit them so the config reflects a
    # checkout-free setup by default. Applied even on re-runs so an existing
    # config gains them when a checkout is passed.
    if args.yue_dir and Path(args.yue_dir, "pyproject.toml").is_file():
        raw.setdefault("yue2", {})
        raw["yue2"]["dir"] = args.yue_dir
        print(f"yue2.dir -> {args.yue_dir}")
    if args.sheetsage_dir and Path(args.sheetsage_dir, "modeling_mert2.py").is_file():
        raw.setdefault("sheetsage2", {})
        raw["sheetsage2"]["dir"] = args.sheetsage_dir
        print(f"sheetsage2.dir -> {args.sheetsage_dir}")
    if args.strict_pins:
        raw.setdefault("worker", {})
        raw["worker"]["python_sheetsage2"] = strict_python_path(args.root)
        print(f"strict-pins: worker.python_sheetsage2 -> {raw['worker']['python_sheetsage2']}")
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
