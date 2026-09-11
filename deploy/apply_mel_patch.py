#!/usr/bin/env python3
"""Apply the vendored mel-frontend patch to a SheetSage2 model directory.

Creates data/sheetsage2-model/ (cwd-relative or --data-dir): a complete local
model (patched code + weights) whose MERT2 mel frontend imports
deploy/patches/mert2_transforms.py instead of torchaudio.transforms.

Use this only when torchaudio cannot be installed for your torch build; the
default shared-venv install pairs the latest torch with torchaudio 2.11+, which
works with every future torch release, and no patch is needed.

No repository checkout is required: if a SheetSage2 checkout is given/available
it is used as the code source; otherwise the code is fetched from the HF Hub
(m-a-p/SheetSage2) and patched in place. Weights are downloaded from the Hub
(either way) with snapshot_download(local_dir=...). Finally, sheetsage2.model in
config.yaml is updated to the patched model path.

Your SheetSage2 repository checkout is left untouched.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH_TARGET = "mert2_transforms.py"
IMPORT_LINE = "from torchaudio.transforms import AmplitudeToDB, MelScale, Spectrogram"
PATCHED_IMPORT = "from mert2_transforms import AmplitudeToDB, MelScale, Spectrogram"
CODE_SUFFIXES = {".py", ".json"}
COPY_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md", "__init__.py")
HUB_CODE_PATTERNS = ["*.py", "*.json", "LICENSE", "THIRD_PARTY_NOTICES.md", "__init__.py"]
HUB_WEIGHT_PATTERNS = ["*.safetensors", "*model-?????-of-?????.safetensors"]


def load_yaml_module():
    import yaml  # noqa: F401
    return sys.modules["yaml"]


def find_sheetsage_dir(explicit: str | None) -> Path | None:
    """Only an explicitly passed checkout is used; otherwise the Hub provides the code."""
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not (path / "modeling_mert2.py").is_file():
            raise SystemExit(f"{path} does not look like the SheetSage2 checkout (missing modeling_mert2.py)")
        return path
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheetsage-dir", default=None, help="SheetSage2 checkout (opt-in; the HF Hub is used otherwise)")
    parser.add_argument("--data-dir", default=None, help="project data dir (default: <root>/data)")
    parser.add_argument("--config", default=None, help="config.yaml path (default: <root>/config.yaml)")
    parser.add_argument("--revision", default=None, help="pin the Hub weight download to the source revision")
    args = parser.parse_args()

    yaml = load_yaml_module()
    source = find_sheetsage_dir(args.sheetsage_dir)
    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else ROOT / "data"
    target = data_dir / "sheetsage2-model"
    target.mkdir(parents=True, exist_ok=True)

    # 1. Code source: a local checkout if one is available, else the HF Hub.
    if source is not None:
        for path in sorted(source.iterdir()):
            if path.is_file() and (path.suffix in CODE_SUFFIXES or path.name in COPY_FILES):
                shutil.copyfile(path, target / path.name)
    else:
        pooled = any((target / name).is_file() for name in ("modeling_mert2.py", "modeling_sheetsage2.py"))
        if not pooled:
            print("No SheetSage2 checkout found; fetching code from m-a-p/SheetSage2 ...", flush=True)
            from huggingface_hub import snapshot_download
            code_kwargs = {"local_dir": str(target), "allow_patterns": HUB_CODE_PATTERNS}
            if args.revision:
                code_kwargs["revision"] = args.revision
            snapshot_download("m-a-p/SheetSage2", **code_kwargs)

    # 2. Write the vendored transforms next to the patched code.
    if not (target / "modeling_mert2.py").is_file():
        raise SystemExit("modeling_mert2.py missing from the target; the code source "
                         "(checkout or Hub download) failed")
    patch_path = ROOT / "deploy" / "patches" / PATCH_TARGET
    shutil.copyfile(patch_path, target / PATCH_TARGET)

    # 3. Patch the import in modeling_mert2.py (and idempotently in any other file that uses it).
    patched_files = []
    for path in sorted(target.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if IMPORT_LINE in text:
            path.write_text(text.replace(IMPORT_LINE, PATCHED_IMPORT), encoding="utf-8")
            patched_files.append(path.name)
    if not patched_files:
        existing = (target / "modeling_mert2.py").read_text(encoding="utf-8")
        if PATCHED_IMPORT not in existing:
            raise SystemExit("could not patch modeling_mert2.py: import line not found and patch not applied")

    # 4. Weights: reuse if present, else download from the Hub (weights only).
    from huggingface_hub import snapshot_download

    if not any(target.glob("*.safetensors")):
        print("Downloading SheetSage2 weights from m-a-p/SheetSage2 ...", flush=True)
        kwargs = {"local_dir": str(target), "allow_patterns": HUB_WEIGHT_PATTERNS}
        if args.revision:
            kwargs["revision"] = args.revision
        snapshot_download("m-a-p/SheetSage2", **kwargs)
    has_weights = any(target.glob("*.safetensors"))
    if not has_weights:
        raise SystemExit("no safetensors weights found after download; check your network")

    # 5. Point config.yaml at the patched model.
    config_path = Path(args.config).expanduser().resolve() if args.config else ROOT / "config.yaml"
    raw = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        raw = loaded if isinstance(loaded, dict) else {}
    raw.setdefault("sheetsage2", {})
    raw["sheetsage2"]["model"] = str(target)
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")

    print(f"Patched model ready: {target}")
    print(f"Patched files: {', '.join(patched_files) or '(already patched)'}")
    print(f"config.yaml updated: sheetsage2.model -> {target}")
    print("Note: use the 'default' transcription preset with this patch; \"paper\" needs torchaudio.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
