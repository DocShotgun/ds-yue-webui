"""NDJSON worker protocol shared by the model workers (standard library only).

A worker process boots with a spec file, loads its model once and stays alive
across jobs. The server writes one JSON command per line to stdin:

    {"cmd": "run", "job_id": 1, ...}      queue a job (executed one at a time)
    {"cmd": "release"}                    unload the model, keep the process warm
    {"cmd": "shutdown"}                   drain the queue and exit

The worker writes one JSON event per line to stdout:

    {"event": "loading"}                  loading the model
    {"event": "ready"}                    model loaded, idle
    {"event": "running", "job_id": N}     job started
    {"event": "progress", "job_id": N,...} job progress (stage/done/total/tokens)
    {"event": "job_done", "job_id": N, "result": {...}}
    {"event": "job_failed", "job_id": N, "error": ..., "type": ...}
    {"event": "job_cancelled", "job_id": N}
    {"event": "released"}                 model unloaded, idle
    {"event": "engine_stopped"}

SIGTERM (graceful cancel) sets a cancel flag checked by the running job; the
worker stays alive afterwards. A sudden death is detected by the server via EOF.
"""
from __future__ import annotations

import importlib.util
import json
import signal
import sys
import threading
from collections import deque
from pathlib import Path
import os


class Events:
    """Thread-safe NDJSON event writer."""

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.lock = threading.Lock()

    def write(self, event: str, **fields) -> None:
        line = json.dumps({"event": event, **fields}, default=str, ensure_ascii=False)
        with self.lock:
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass

    def progress(self, job_id: int, **fields) -> None:
        self.write("progress", job_id=job_id, **fields)


def load_module(path: str | Path):
    """Import a Python file by path without any package installation."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    name = "ds_yue_worker_" + path.stem.replace("-", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _install_cancel_handler(cancel: threading.Event) -> None:
    def handler(signum, frame):
        cancel.set()

    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        pass


def serve(boot: dict, load_fn, run_fn, release_fn) -> int:
    """Run the resident worker loop. load_fn(spec)->obj; run_fn(obj, command, events,
    cancelled)->result; release_fn(obj).

    The model loads eagerly at boot; the server sends its first job only after
    the "ready" event. After "release" the next job triggers a reload.
    """
    events = Events()
    cancel = threading.Event()
    _install_cancel_handler(cancel)
    state = {"shutdown": False, "busy": False}
    queue: deque = deque()
    cv = threading.Condition()
    obj = None

    try:
        events.write("loading")
        obj = load_fn(boot)
        events.write("ready")
    except BaseException as exc:
        events.write("boot_failed", error=str(exc) or type(exc).__name__, type=type(exc).__name__)
        return 1

    def engine() -> None:
        nonlocal obj
        while True:
            with cv:
                while not queue and not state["shutdown"]:
                    cv.wait()
                item = queue.popleft() if queue else None
            if item is None:
                break
            job_id, command = item
            cancel.clear()
            state["busy"] = True
            try:
                if obj is None:
                    events.write("loading")
                    obj = load_fn(boot)
                    events.write("ready")
                events.write("running", job_id=job_id)
                result = run_fn(obj, command, events, lambda: cancel.is_set())
                events.write("job_done", job_id=job_id, result=result)
            except (InterruptedError, KeyboardInterrupt):
                events.write("job_cancelled", job_id=job_id)
            except BaseException as exc:
                events.write("job_failed", job_id=job_id,
                             error=str(exc) or type(exc).__name__, type=type(exc).__name__)
            finally:
                state["busy"] = False
        events.write("engine_stopped")

    worker_thread = threading.Thread(target=engine, daemon=True, name="worker-engine")
    worker_thread.start()

    while True:
        try:
            line = sys.stdin.readline()
        except OSError:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except ValueError:
            events.write("error", message="invalid command: not JSON")
            continue
        if not isinstance(command, dict):
            events.write("error", message="invalid command: not an object")
            continue
        cmd = command.get("cmd")
        if cmd == "run":
            job_id = command.get("job_id")
            if not isinstance(job_id, int):
                events.write("error", message="run command requires an integer job_id")
                continue
            with cv:
                queue.append((job_id, command))
                cv.notify()
        elif cmd == "release":
            if state["busy"] or queue:
                events.write("error", message="worker busy; release when idle")
            elif obj is None:
                events.write("released")
            else:
                try:
                    release_fn(obj)
                except BaseException as exc:
                    events.write("error", message=f"release failed: {exc}")
                    continue
                obj = None
                events.write("released")
        elif cmd == "shutdown":
            with cv:
                state["shutdown"] = True
                cv.notify()
            break
        else:
            events.write("error", message=f"unknown command: {cmd!r}")

    with cv:
        state["shutdown"] = True
        cv.notify()
    worker_thread.join(timeout=120)
    return 0
