#!/usr/bin/env python3
"""SheetSage2 resident model worker: audio -> ABC lead-sheet transcription.

Runs inside the venv that satisfies SheetSage2's needs (shared venv by default,
.venv-sheetsage2 in --strict-pins mode). Loads the model via transformers
AutoModel (trust_remote_code) exactly as the official skill script does, and
streams the transcriber's own progress callbacks over the worker protocol.

Commands arrive over stdin/stdout using the protocol in protocol.py.
Note: the model.transcribe API has no cancellation hook, so a running
transcription can only be cancelled by stopping the worker process.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import serve, load_module  # noqa: E402

_BOOT: dict = {}


def abc_tools() -> object:
    """Resolution order: boot spec, YUE_WEBUI_YUE2_DIR env, vendored copy, YuE checkout."""
    candidates = []
    spec_tools = _BOOT.get("abc_tools")
    if spec_tools:
        candidates.append(Path(spec_tools))
    env_dir = os.environ.get("YUE_WEBUI_YUE2_DIR")
    if env_dir:
        path = Path(env_dir)
        base = path if (path / "skills").is_dir() else path.parent
        candidates.append(base / "skills" / "yue2-music" / "scripts" / "abc_tools.py")
    root = Path(__file__).resolve().parents[1]
    candidates.append(root / "vendor" / "yue2" / "abc_tools.py")
    candidates.append((root / ".." / "YuE").resolve() / "skills" / "yue2-music" / "scripts" / "abc_tools.py")
    for candidate in candidates:
        if candidate.is_file():
            return load_module(candidate)
    raise FileNotFoundError("abc_tools.py not found; pass abc_tools in the boot spec, "
                            "set YUE_WEBUI_YUE2_DIR, or vendor it under vendor/yue2/")


def load_model(spec: dict):
    import torch
    from transformers import AutoModel

    torch.set_num_threads(int(spec.get("threads", 4)))
    device = spec.get("device", "cuda")
    loader = dict(trust_remote_code=True, local_files_only=bool(spec.get("offline", False)))
    revision = spec.get("revision")
    if revision:
        loader.update(revision=revision, code_revision=revision)
    base_model = spec.get("base_model")
    if base_model:
        loader["base_model_path"] = base_model
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available for the SheetSage2 worker; set sheetsage2.device to cpu")
    model = AutoModel.from_pretrained(spec["model"], **loader).eval().to(device)
    return model


def release_model(model) -> None:
    import gc

    import torch

    model.to("cpu")
    del model
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def prompts_for_task(task: str) -> tuple[list[str], bool]:
    prompts = ["timestamp", "downbeat_meter", "structure", "key"]
    if task == "full":
        prompts += ["chord_full", "melody_full"]
        return prompts, False
    prompts += ["melody_vocal" if task == "melody-vocal" else "melody_full"]
    return prompts, True


def _jsonable(report: dict):
    from fractions import Fraction

    def convert(value):
        if isinstance(value, Fraction):
            return str(value)
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value

    return convert(report)


def run_sheetsage(model, command: dict, events, cancelled) -> dict:
    job_id = command["job_id"]
    output_dir = Path(command["output_dir"])
    task = command.get("task", "full")
    preset = command.get("preset", "default")
    prompts, melody_only = prompts_for_task(task)
    audio = command["audio"]

    def report(fields: dict) -> None:
        events.progress(job_id, **{key: value for key, value in fields.items()
                                   if key in {"stage", "window", "windows", "tokens"}})

    result = model.transcribe(
        str(audio), output_dir=str(output_dir), prompts=prompts,
        dtype=command.get("dtype", "bf16"), preset=preset, max_seconds=command.get("max_seconds"),
        progress=report, melody_only=melody_only)
    if result.get("abc_error") or not result.get("abc"):
        raise ValueError(f"Transcription produced no usable ABC: {result.get('abc_error')}")
    tools = abc_tools()
    score = tools.parse_abc(result["abc"])
    if melody_only and any(bool(v.chords) for v in score.voices.values()):
        raise ValueError("Melody transcription contains unexpected chord symbols")
    if not (output_dir / "score.abc").is_file():
        raise ValueError("Transcriber did not save score.abc")
    (output_dir / "abc_check.json").write_text(json.dumps(
        {"status": "passed", "score": _jsonable(tools.report(score)),
         "scope": "symbolic format; transcription accuracy still needs review"}, indent=2) + "\n",
        encoding="utf-8")
    (output_dir / "transcription_manifest.json").write_text(json.dumps(
        {"status": "complete", "warnings": result.get("warnings", []),
         "model": {"requested": _BOOT.get("model"), "offline": _BOOT.get("offline", False),
                   "device": _BOOT.get("device"), "dtype": _BOOT.get("dtype", "bf16")},
         "packages": _package_versions(),
         "artifacts": {}, "task": task, "preset": preset}, indent=2) + "\n",
        encoding="utf-8")
    return {"status": "complete", "output": str(output_dir), "warnings": result.get("warnings", []),
            "seconds": result.get("elapsed_seconds"), "abc": True, "task": task}


def _package_versions() -> dict:
    versions = {}
    for name in ("torch", "transformers", "huggingface-hub", "numpy"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def transcribe_once(spec: dict) -> int:
    """One-shot transcription for install.sh --smoke (no resident loop)."""
    from pathlib import Path

    output_dir = Path(spec["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    command = {"job_id": 0, "output_dir": str(output_dir), "audio": spec["audio"],
               "task": spec.get("task", "full"), "preset": spec.get("preset", "default"),
               "max_seconds": spec.get("max_seconds"), "dtype": spec.get("dtype", "bf16")}
    model = load_model(spec)
    try:
        result = run_sheetsage(model, command, Events(), lambda: False)
    except BaseException as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "error": str(exc)}))
        return 1
    (output_dir / "smoke_status.json").write_text(json.dumps(result, default=str, indent=2) + "\n",
                                                  encoding="utf-8")
    print(json.dumps({"status": result.get("status"), "output": result.get("output"),
                      "warnings": result.get("warnings", []), "seconds": result.get("seconds")}, default=str))
    return 0


def main() -> int:
    global _BOOT
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve", help="resident worker loop")
    serve_parser.add_argument("--spec", required=True, help="boot spec JSON file")
    smoke_parser = sub.add_parser("smoke", help="one-shot transcription for install.sh --smoke")
    smoke_parser.add_argument("--spec", required=True, help="smoke spec JSON file")
    args = parser.parse_args()
    with open(args.spec, "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    if args.command == "smoke":
        return transcribe_once(spec)
    _BOOT = spec
    return serve(_BOOT, load_model, run_sheetsage, release_model)


if __name__ == "__main__":
    sys.exit(main())
