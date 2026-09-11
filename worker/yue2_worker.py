#!/usr/bin/env python3
"""YuE2 resident model worker (serve) plus a one-shot smoke generator.

Runs inside the shared venv. Uses only the public YuE2Pipeline API:

  pipe(...)          one-call generation with on_token progress and cancellation
  pipe.plan(...)     plan-only runs (saved plan directory)

Commands arrive over stdin/stdout using the protocol in protocol.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import serve  # noqa: E402


def load_pipeline(spec: dict):
    # Windows: importing the transformers/scipy DLL stack from a thread other
    # than the main one deadlocks on the loader lock (scipy.linalg.blas during
    # module import). The runtime loads lazily on first use, so force the load
    # here; load_pipeline runs on the main thread at boot (protocol.py makes
    # sure the after-release reload runs on the main thread too).
    import numpy  # noqa: F401
    import soundfile  # noqa: F401
    from yue2 import YuE2Pipeline

    budget = float(spec.get("budget", 24.0))
    pipeline = YuE2Pipeline.from_pretrained(
        spec["model"], vae=spec.get("vae", "m-a-p/YuE2-Vae"),
        device=spec.get("device", "auto"), memory_budget_gib=budget,
        backend=spec.get("backend", "torch"), quantization=spec.get("quantization", "none"),
        offload_ar=bool(spec.get("offload_ar", False)), local_files_only=bool(spec.get("offline", False)),
        vae_core_frames=512 if budget <= 12 else 1024,
        progress=True)
    # the torch AR model is genuinely unused only when backend=vllm AND vllm is
    # importable; otherwise generate falls back to torch, and the warm-up load
    # must not be left to the engine thread (Windows import deadlock)
    if spec.get("backend", "torch") != "vllm" or importlib.util.find_spec("vllm") is None:
        pipeline._load_model()
    return pipeline


def release_pipeline(pipe) -> None:
    pipe.close()


def run_yue2(pipe, command: dict, events, cancelled) -> dict:
    action = command["action"]
    job_id = command["job_id"]
    request = dict(command.get("request") or {})
    output_dir = Path(command["output_dir"])
    sampling = command.get("sampling") or {}
    counter = [0]
    reported = [0]

    def on_token(phase, token):
        counter[0] += 1
        if counter[0] - reported[0] >= 25:
            reported[0] = counter[0]
            events.progress(job_id, stage=phase, tokens=counter[0])

    if action == "generate":
        started = time.monotonic()
        result = pipe(**request, abc_sampling=sampling.get("abc"),
                      semantic_sampling=sampling.get("semantic"),
                      cancelled=cancelled, on_token=on_token)
        receipt = result.save_artifacts(output_dir)
        # timing keys per stage; no single e2e total exists, so sum + wall fallback
        timing = result.timing
        seconds = sum(timing.get(key) or 0.0 for key in
                      ("resolve_and_integrity_seconds", "mot_load_seconds",
                       "nar_seconds", "vae_seconds"))
        return {"status": "complete", "output": str(output_dir), "truncated": result.truncated,
                "seconds": seconds if seconds > 0 else time.monotonic() - started,
                "audio_seconds": receipt.get("audio_seconds"),
                "identity": receipt.get("identity"),
                "wall_seconds": time.monotonic() - started}
    if action == "plan":
        plan = pipe.plan(**request, abc_sampling=sampling.get("abc"),
                         cancelled=cancelled, on_token=on_token)
        plan.save(output_dir)
        timing = plan.timing
        seconds = timing.get("plan_seconds") or sum(timing.get(key) or 0.0 for key in
                                                    ("resolve_and_integrity_seconds", "mot_load_seconds"))
        return {"status": "complete", "stage": "plan", "output": str(output_dir),
                "truncated": plan.truncated, "seconds": seconds or plan.timing.get("seconds", 0.0)}
    if action == "decode":
        return run_decode(pipe, command, output_dir)
    raise ValueError(f"Unknown yue2 action: {action!r}")


def run_decode(pipe, command: dict, output_dir: Path) -> dict:
    """Re-decode cached latents from a library song, optionally with the legacy VAE."""
    import numpy as np
    import soundfile as sf
    from yue2 import SymbolicPlan
    from yue2.storage import collect_hashes

    source_dir = Path(command["source_dir"])
    for name in ("result.json", "semantic.npy", "latent.npy"):
        if not (source_dir / name).is_file():
            raise ValueError(f"Source song is missing {name}: {source_dir}")
    source_result = json.loads((source_dir / "result.json").read_text())
    if source_result.get("status") != "complete":
        raise ValueError("Source song did not complete")
    source_mot = (source_result.get("weights") or {}).get("mot")
    if source_mot is not None and source_mot != pipe.weights.get("mot"):
        raise ValueError("Source song was generated by a different YuE2 model; "
                         "semantic tokens are model-specific")
    plan = SymbolicPlan.load(source_dir)
    semantic = np.load(source_dir / "semantic.npy", allow_pickle=False)
    latents = np.load(source_dir / "latent.npy", allow_pickle=False)
    vae = command.get("vae") or "standard"
    started = time.monotonic()
    if vae == "standard":
        audio = pipe.decode(latents)
    else:
        from yue2.storage import resolve_model
        audio = pipe.decode(latents, vae=str(resolve_model(vae, local_files_only=bool(command.get("offline", False)))))
    output_dir.mkdir(parents=True, exist_ok=True)
    plan.save(output_dir)
    np.save(output_dir / "semantic.npy", semantic.astype(np.int32))
    np.save(output_dir / "latent.npy", latents.astype(np.float32))
    request = json.loads((source_dir / "request.json").read_text())
    (output_dir / "request.json").write_text(json.dumps(request, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sf.write(output_dir / "audio.flac", audio, 48000, subtype="PCM_24")
    result = {"status": "complete", "output": str(output_dir), "decoder": "standard" if vae == "standard" else vae,
              "sources": {"song": source_dir.name, "result_json": "decoded from cached latents"},
              "sample_rate": 48000, "audio_seconds": len(audio) / 48000,
              "seconds": time.monotonic() - started, "artifacts": collect_hashes(output_dir)}
    (output_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def smoke(spec: dict) -> int:
    """Opt-in smoke generator: one small generate with token-capped sampling."""
    pipe = load_pipeline(spec)
    started = time.monotonic()
    try:
        output_dir = Path(spec["output_dir"])
        request = dict(spec["request"])
        sampling = spec.get("sampling") or {}
        result = pipe(**request, abc_sampling=sampling.get("abc"),
                      semantic_sampling=sampling.get("semantic"))
        receipt = result.save_artifacts(output_dir)
    except BaseException as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "error": str(exc)}))
        return 1
    finally:
        pipe.close()
    print(json.dumps({"status": "complete", "output": str(output_dir),
                      "truncated": result.truncated, "wall_seconds": time.monotonic() - started,
                      "audio_seconds": receipt.get("audio_seconds")}, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve_parser = sub.add_parser("serve", help="resident worker loop")
    serve_parser.add_argument("--spec", required=True, help="boot spec JSON file")
    smoke_parser = sub.add_parser("smoke", help="one-shot token-capped generate for install.sh --smoke")
    smoke_parser.add_argument("--spec", required=True, help="smoke spec JSON file")
    args = parser.parse_args()
    if args.command == "smoke":
        with open(args.spec, "r", encoding="utf-8") as handle:
            return smoke(json.load(handle))
    with open(args.spec, "r", encoding="utf-8") as handle:
        boot = json.load(handle)
    return serve(boot, load_pipeline, run_yue2, release_pipeline)


if __name__ == "__main__":
    sys.exit(main())
