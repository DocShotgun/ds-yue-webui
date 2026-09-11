"""Import-and-protocol tests of the real worker scripts (GPU-free, no model load).

The mock-worker tests replace the real scripts, so they cannot catch duplicate
definitions or signature drift inside the workers themselves. These tests
import the real modules (torch/transformers are imported lazily inside the
functions) and drive protocol.serve() with fake load/run/release functions.
"""
import importlib.util
import io
import json
import os
import re
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _definition_counts(worker_dir: Path, script: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in (worker_dir / f"{script}.py").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*def\s+([A-Za-z_]\w*)\s*\(", line)
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return counts


def test_sheetsage_worker_surface():
    """Regression test for the live-worker bug where a stale duplicate
    definition of abc_tools(env) shadowed the new zero-arg version."""
    import inspect

    script = "sheetsage_worker"
    module = _load(f"test_{script}", REPO_ROOT / "worker" / f"{script}.py")
    assert not inspect.signature(module.abc_tools).parameters, "abc_tools must take no arguments"
    counts = _definition_counts(REPO_ROOT / "worker", script)
    for name in ("abc_tools", "load_model", "release_model", "prompts_for_task",
                 "run_sheetsage", "transcribe_once", "main"):
        assert counts.get(name, 0) <= 1, f"{script}: {name} defined {counts.get(name, 0)} times"
        assert callable(getattr(module, name, None)), f"{script}: {name} missing or not callable"
    assert not hasattr(module, "DEFAULT_ABC_TOOLS"), "stale constant left behind"


def test_yue2_worker_surface():
    script = "yue2_worker"
    module = _load(f"test_{script}", REPO_ROOT / "worker" / f"{script}.py")
    counts = _definition_counts(REPO_ROOT / "worker", script)
    for name in ("load_pipeline", "release_pipeline", "run_yue2", "run_decode", "smoke", "main"):
        assert counts.get(name, 0) <= 1, f"{script}: {name} defined {counts.get(name, 0)} times"
        assert callable(getattr(module, name, None)), f"{script}: {name} missing or not callable"


def test_worker_protocol_handshake():
    """serve() must emit loading then ready at boot, run a queued job exactly
    once (running -> progress -> job_done), and stop cleanly on shutdown."""
    protocol = _load("test_worker_protocol", REPO_ROOT / "worker" / "protocol.py")

    def load_fn(spec):
        assert spec.get("kind") == "fake"
        return {"booted": True}

    def run_fn(obj, command, events, cancelled):
        assert obj is not None and obj.get("booted")
        events.progress(command["job_id"], stage="test")
        import time

        time.sleep(0.6)
        events.progress(command["job_id"], stage="test", done=1, total=1)
        return {"ok": True}

    def release_fn(obj):
        pass

    stdout = io.StringIO()
    stdin_r, stdin_w = os.pipe()
    old_stdin, old_stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = os.fdopen(stdin_r, "r"), stdout
    try:
        def feed():
            try:
                for line in (json.dumps({"cmd": "run", "job_id": 1}),
                             json.dumps({"cmd": "release"}),
                             json.dumps({"cmd": "shutdown"})):
                    os.write(stdin_w, (line + "\n").encode())
                    import time

                    time.sleep(0.25)
            finally:
                os.close(stdin_w)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        assert protocol.serve({"kind": "fake"}, load_fn, run_fn, release_fn) == 0
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout

    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    kinds = [item["event"] for item in events]
    assert kinds[0] == "loading" and kinds[1] == "ready", kinds
    assert kinds[-1] == "engine_stopped", kinds
    running_idx = kinds.index("running")
    assert "job_done" in kinds and kinds.index("job_done") > running_idx, kinds
    assert "job_failed" not in kinds and "job_cancelled" not in kinds, kinds
    assert [item["job_id"] for item in events if "job_id" in item and item["event"] == "job_done"] == [1]
    busy_errors = [item for item in events
                   if item["event"] == "error" and "busy" in item.get("message", "")]
    assert len(busy_errors) == 1, "release while busy must fail exactly once"
    assert events.index(busy_errors[0]) > running_idx, "busy error must come after the run started"


def test_worker_protocol_boot_failure():
    protocol = _load("test_worker_protocol_bf", REPO_ROOT / "worker" / "protocol.py")

    def load_fn(spec):
        raise RuntimeError("no model here")

    stdout = io.StringIO()
    stdin_r, stdin_w = os.pipe()
    old_stdin, old_stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = os.fdopen(stdin_r, "r"), stdout
    try:
        os.write(stdin_w, json.dumps({"cmd": "shutdown"}).encode() + b"\n")
        os.close(stdin_w)
        assert protocol.serve({}, load_fn, lambda o: None, lambda o: None) == 1
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert events == [{"event": "loading"},
                      {"event": "boot_failed", "error": "no model here", "type": "RuntimeError"}]
