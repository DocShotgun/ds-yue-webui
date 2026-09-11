"""Diagnostics report: doctor, GPU, tools, disk, queue, smoke test."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _doctor_report(settings) -> dict:
    """Prefer the in-process yue2 doctor; fall back to the worker env as a subprocess."""
    try:
        import yue2.cli  # noqa: F401
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            yue2.cli.main(["doctor"])
        return json.loads(buffer.getvalue())
    except ImportError:
        pass
    except Exception as exc:
        return {"unavailable": f"in-process doctor failed: {type(exc).__name__}: {exc}"}
    try:
        result = subprocess.run(
            [str(settings.worker_python_yue2), "-m", "yue2", "doctor"],
            capture_output=True, text=True, timeout=180, check=False,
            cwd=str(settings.root))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"unavailable": f"doctor subprocess failed: {type(exc).__name__}: {exc}"}
    if result.returncode not in (0, 1):
        return {"unavailable": "doctor failed; see logs/worker-yue2.log",
                "stderr": result.stderr[-1200:] if result.stderr else None}
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"unavailable": "doctor produced no JSON", "stdout": result.stdout[-1200:]}


def _gpu_report() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            gpus.append({"name": parts[0], "memory_used_mib": float(parts[1]),
                         "memory_total_mib": float(parts[2]), "utilization_percent": float(parts[3])})
        except ValueError:
            continue
    return gpus


def _tool_versions() -> dict:
    def version(command: list[str]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        first = (result.stdout or "").strip().splitlines()
        return first[0] if first else None

    return {"ffmpeg": version(["ffmpeg", "-version"]), "uv": version(["uv", "--version"])}


def build_report(settings, queue) -> dict:
    disk = shutil.disk_usage(settings.data_dir)
    workers = queue.workers_snapshot()
    loaded = [worker for worker in workers if worker.get("model_state") == "ready"]
    warnings = []
    if settings.residency == "always" and len(loaded) > 1:
        warnings.append("residency=always with both model families loaded; VRAM is tight "
                        "(YuE2 model+VAE plus SheetSage2). Switch to on-demand if you hit OOM.")
    for worker in workers:
        if worker.get("alive") and worker.get("model_state") is None:
            warnings.append(f"{worker['kind']} worker is starting up or its model failed to load; "
                            f"see {worker['log']}")
    smoke = None
    if settings.smoke_result_path.is_file():
        try:
            smoke = json.loads(settings.smoke_result_path.read_text(encoding="utf-8"))
        except ValueError:
            smoke = {"unavailable": "smoke-result.json is not valid JSON"}
    ffmpeg = _tool_versions()["ffmpeg"]
    if ffmpeg is None:
        warnings.append("ffmpeg not found on PATH; transcription (default preset) and mp3 delivery need it")
    resolved_abc_tools = settings.resolved_abc_tools_path()
    return {
        "generated_at": time.time(),
        "python": sys.version,
        "config": settings.snapshot(),
        "doctor": _doctor_report(settings),
        "gpus": _gpu_report(),
        "tools": _tool_versions(),
        "disk": {"total_gib": disk.total / 2**30, "free_gib": disk.free / 2**30},
        "queue": {"counts": queue.counts(), "workers": workers},
        "smoke": smoke,
        "warnings": warnings,
        "abc_tools": {"available": resolved_abc_tools.is_file(),
                      "path": str(resolved_abc_tools),
                      "vendored": resolved_abc_tools == settings.vendored_abc_tools_path},
    }
