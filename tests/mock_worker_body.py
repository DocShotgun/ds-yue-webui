"""Mock model workers with the same NDJSON protocol as the real ones (GPU-free)."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.getcwd()) / "worker"))
from protocol import serve, Events  # noqa: E402


def load_fn(spec):
    return {"spec": spec}


def release_fn(obj):
    pass


def run_fn(obj, command, events, cancelled):
    action = command.get("action")
    job_id = command["job_id"]
    output = Path(command["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    for i in range(2):
        if cancelled():
            raise InterruptedError("cancelled by server")
        events.progress(job_id, stage=action, tokens=(i + 1) * 100)
    if action == "generate":
        (output / "result.json").write_text(json.dumps(
            {"status": "complete", "audio_seconds": 10.0,
             "truncated": {"abc": False, "semantic": False},
             "weights": {"mot": "mock"}}), encoding="utf-8")
        return {"status": "complete", "output": str(output), "audio_seconds": 10.0,
                "seconds": 1.0, "identity": "mock-identity"}
    if action == "plan":
        (output / "plan.json").write_text(json.dumps(
            {"request": command.get("request", {}), "truncated": False}), encoding="utf-8")
        return {"status": "complete", "stage": "plan", "output": str(output)}
    if action == "transcribe":
        (output / "score.abc").write_text("X:1\nT:mock\n", encoding="utf-8")
        (output / "abc_check.json").write_text(json.dumps(
            {"status": "passed", "score": {}, "scope": "symbolic format; transcription accuracy still needs review"}),
            encoding="utf-8")
        (output / "transcription_manifest.json").write_text(json.dumps(
            {"status": "complete", "warnings": [], "packages": {}, "artifacts": {}, "task": "full"},
            ), encoding="utf-8")
        return {"status": "complete", "output": str(output), "warnings": [], "seconds": 1.0}
    if action == "decode":
        (output / "result.json").write_text(json.dumps(
            {"status": "complete", "audio_seconds": 9.0, "decoder": command.get("vae", "standard")}),
            encoding="utf-8")
        return {"status": "complete", "output": str(output), "audio_seconds": 9.0}
    raise ValueError(f"unknown action: {action!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--spec", required=True)
    args = parser.parse_args()
    with open(args.spec, "r", encoding="utf-8") as handle:
        boot = json.load(handle)
    return serve(boot, load_fn, run_fn, release_fn)


if __name__ == "__main__":
    sys.exit(main())
