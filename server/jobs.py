"""SQLite-backed job queue, resident model workers, and single-GPU serialization."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from .config import Settings

KINDS = ("generate", "plan", "transcribe", "decode")
TERMINAL = ("done", "failed", "cancelled")


class JobCancelled(Exception):
    """The job was cancelled while running."""


class _Future:
    def __init__(self):
        self.event = threading.Event()
        self.state = None
        self.payload = None
        self.lock = threading.Lock()

    def set(self, state: str, payload):
        with self.lock:
            if self.state is None:
                self.state, self.payload = state, payload
        self.event.set()

    def resolve(self) -> tuple[str, object]:
        self.event.wait()
        return self.state, self.payload


def _spec_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ResidentWorker:
    """A resident model worker process speaking NDJSON over stdin/stdout.

    The process boots with a spec (model/vae/device...), loads its model once and
    stays alive across jobs. A "release" command unloads the model, freeing VRAM
    while keeping the process warm; the next job reloads it on demand.
    """

    READY_TIMEOUT = 1800.0
    RELEASE_TIMEOUT = 600.0

    def __init__(self, kind: str, queue: "JobQueue", python: Path, script: Path):
        self.kind = kind
        self.queue = queue
        self.settings = queue.settings
        self.python = python
        self.script = script
        self.proc: subprocess.Popen | None = None
        self.spec_hash: str | None = None
        self.model_state: str | None = None
        self.busy = 0
        self.last_activity: float | None = None
        self.futures: dict[int, _Future] = {}
        self.state_lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.log_path = self.settings.logs_dir / f"worker-{kind}.log"

    # -- lifecycle -----------------------------------------------------------
    def _boot_path(self) -> Path:
        path = self.settings.specs_dir / f"worker-{self.kind}-boot.json"
        self.settings.specs_dir.mkdir(parents=True, exist_ok=True)
        return path

    def _start(self, spec: dict, spec_hash: str) -> None:
        if self.proc is not None and self.proc.poll() is None and self.spec_hash == spec_hash:
            return
        if self.proc is not None and self.proc.poll() is None:
            self._terminate()
        self.spec_hash = spec_hash
        self.model_state = None
        boot = self._boot_path()
        boot.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(self.python), str(self.script), "serve", "--spec", str(boot)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
            cwd=str(self.settings.root))
        self._append_log(f"\n===== boot {time.strftime('%Y-%m-%dT%H:%M:%S')} pid={self.proc.pid} =====\n")
        threading.Thread(target=self._stderr_pump, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._reader_loop, args=(self.proc, spec_hash), daemon=True).start()

    def ensure(self, spec: dict) -> None:
        spec_hash = _spec_hash(spec)
        with self.state_lock:
            self._start(spec, spec_hash)
        deadline = time.monotonic() + self.READY_TIMEOUT
        while True:
            alive = self.proc is not None and self.proc.poll() is None
            with self.state_lock:
                state = self.model_state
            if not alive:
                raise RuntimeError(f"{self.kind} worker exited during startup; see {self.log_path}")
            if state in ("ready", "released"):
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"{self.kind} worker did not become ready within {self.READY_TIMEOUT:.0f}s")
            time.sleep(0.25)

    def _terminate(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            self._send_line({"cmd": "shutdown"})
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for future in list(self.futures.values()):
            future.set("failed", {"error": "worker was shut down", "type": "WorkerShutdown"})
        self.futures.clear()

    def shutdown(self) -> None:
        with self.state_lock:
            self._terminate()

    # -- job execution -------------------------------------------------------
    def run(self, job_id: int, command: dict) -> object:
        future = _Future()
        with self.state_lock:
            self.futures[job_id] = future
            self.busy += 1
        try:
            self._send_line(command)
        except OSError as exc:
            with self.state_lock:
                self.futures.pop(job_id, None)
                self.busy = max(0, self.busy - 1)
            raise RuntimeError(f"{self.kind} worker is not accepting jobs: {exc}") from exc
        state, payload = future.resolve()
        with self.state_lock:
            self.futures.pop(job_id, None)
            self.busy = max(0, self.busy - 1)
            self.last_activity = time.time()
        if state == "done":
            return payload
        if state == "cancelled":
            raise JobCancelled()
        error = payload.get("error") if isinstance(payload, dict) else str(payload)
        raise RuntimeError(error or "worker job failed")

    def release(self, timeout: float | None = None) -> None:
        timeout = self.RELEASE_TIMEOUT if timeout is None else timeout
        with self.state_lock:
            alive = self.proc is not None and self.proc.poll() is None
            state, busy = self.model_state, self.busy
        if not alive or busy or state in (None, "released"):
            return
        try:
            self._send_line({"cmd": "release"})
        except OSError:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.state_lock:
                if self.model_state == "released":
                    return
                if self.proc is None or self.proc.poll() is not None:
                    return
            time.sleep(0.25)

    def request_cancel(self) -> None:
        """Ask the worker to cancel its current job (SIGTERM; graceful on Linux)."""
        with self.state_lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            proc.terminate()
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except OSError:
            pass

    def kill(self) -> None:
        with self.state_lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.kill()
        except OSError:
            pass

    # -- plumbing ------------------------------------------------------------
    def _send_line(self, line: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise OSError("worker process is not running")
        with self.send_lock:
            proc.stdin.write(json.dumps(line, default=str) + "\n")
            proc.stdin.flush()

    def _append_log(self, text: str) -> None:
        """Append to the worker log with a short-lived handle, so the file is
        only briefly locked and clearing the history can always delete it."""
        try:
            with open(self.log_path, "a", encoding="utf-8", buffering=1) as handle:
                handle.write(text)
        except OSError:
            pass

    def _stderr_pump(self, proc) -> None:
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._append_log(line if line.endswith("\n") else line + "\n")
        except (OSError, ValueError):
            pass

    def _reader_loop(self, proc, spec_hash) -> None:
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            for raw in stream:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                self._dispatch(payload)
        except (OSError, ValueError):
            pass
        finally:
            self._on_worker_exit(proc, spec_hash)

    def _dispatch(self, payload: dict) -> None:
        event = payload.get("event")
        if event in ("ready", "released"):
            with self.state_lock:
                self.model_state = event
                self.last_activity = time.time()
            return
        if event == "progress":
            job_id = payload.get("job_id")
            if isinstance(job_id, int):
                fields = {key: value for key, value in payload.items()
                          if key not in {"event", "job_id"}}
                self.queue.update_progress(job_id, fields)
            return
        if event in ("job_done", "job_failed", "job_cancelled"):
            job_id = payload.get("job_id")
            if not isinstance(job_id, int):
                return
            future = self.futures.get(job_id)
            if future is None:
                return
            if event == "job_done":
                future.set("done", payload.get("result") or {})
            elif event == "job_failed":
                future.set("failed", {"error": str(payload.get("error", "unknown error")),
                                      "type": str(payload.get("type", "Error"))})
            else:
                future.set("cancelled", None)

    def _on_worker_exit(self, proc, spec_hash) -> None:
        """Only act when the *current* worker died; stale reader threads from a
        replaced worker must not clobber the new worker's state."""
        with self.state_lock:
            if self.proc is not proc or self.spec_hash != spec_hash:
                return
            self.model_state = None
            futures = list(self.futures.items())
            self.futures.clear()
            self.busy = 0
        for job_id, future in futures:
            future.set("failed", {"error": f"{self.kind} worker exited unexpectedly; see {self.log_path}",
                                  "type": "WorkerGone"})

    def snapshot(self) -> dict:
        with self.state_lock:
            return {"kind": self.kind,
                    "pid": self.proc.pid if self.proc is not None else None,
                    "alive": self.proc is not None and self.proc.poll() is None,
                    "model_state": self.model_state,
                    "busy": self.busy,
                    "spec_hash": (self.spec_hash or "")[:12],
                    "log": str(self.log_path),
                    "idle_seconds": (time.time() - self.last_activity) if self.last_activity else None}


class JobQueue:
    """Serial GPU job queue. One job runs at a time across both worker families."""

    def __init__(self, settings: Settings):
        self.settings = settings
        settings.ensure_dirs()
        self.db_path = settings.data_dir / "jobs.db"
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db_lock = threading.RLock()
        with self.db_lock:
            self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                name TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                params TEXT NOT NULL,
                output_dir TEXT,
                error TEXT,
                progress TEXT,
                result TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at REAL, started_at REAL, finished_at REAL)""")
            self.db.commit()
        script_dir = settings.root / "worker"
        self.yue2 = ResidentWorker("yue2", self, settings.worker_python_yue2,
                                   script_dir / "yue2_worker.py")
        self.sheetsage = ResidentWorker("sheetsage", self, settings.worker_python_sheetsage2,
                                        script_dir / "sheetsage_worker.py")
        self.wake = threading.Condition()
        self.cancel_timers: dict[int, threading.Timer] = {}
        self._recover()
        self._loop_thread = threading.Thread(target=self._loop, daemon=True, name="job-queue")
        self._loop_thread.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog, daemon=True, name="idle-release")
        self._watchdog_thread.start()

    # -- persistence ---------------------------------------------------------
    def _row(self, row) -> dict | None:
        if row is None:
            return None
        job = {"id": row[0], "kind": row[1], "name": row[2], "status": row[3], "output_dir": row[5],
               "error": row[6], "created_at": row[10], "started_at": row[11], "finished_at": row[12],
               "deleted": bool(row[9])}
        try:
            job["params"] = json.loads(row[4])
        except ValueError:
            job["params"] = {}
        job["progress"] = json.loads(row[7]) if row[7] else None
        job["result"] = json.loads(row[8]) if row[8] else None
        return job

    def _recover(self) -> None:
        with self.db_lock:
            self.db.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE status IN ('running','pending')",
                            ("server restarted; the job was interrupted", time.time()))
            self.db.commit()

    def update_progress(self, job_id: int, fields: dict) -> None:
        with self.db_lock:
            self.db.execute("UPDATE jobs SET progress=? WHERE id=? AND status='running'",
                            (json.dumps(fields, default=str), job_id))
            self.db.commit()

    def _update(self, job_id: int, **fields) -> dict | None:
        sets, values = [], []
        for key, value in fields.items():
            sets.append(f"{key}=?")
            values.append(json.dumps(value, default=str) if key in ("result", "params", "progress") else value)
        with self.db_lock:
            self.db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", (*values, job_id))
            self.db.commit()
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(row)

    def get(self, job_id: int) -> dict | None:
        with self.db_lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(row)

    def list(self, limit: int = 50) -> list[dict]:
        with self.db_lock:
            rows = self.db.execute(
                "SELECT * FROM jobs WHERE deleted=0 ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def counts(self) -> dict:
        with self.db_lock:
            rows = self.db.execute("SELECT status, COUNT(*) FROM jobs WHERE deleted=0 GROUP BY status").fetchall()
        return {status: count for status, count in rows}

    def mark_deleted(self, job_id: int) -> None:
        with self.db_lock:
            self.db.execute("UPDATE jobs SET deleted=1 WHERE id=?", (job_id,))
            self.db.commit()

    def clear(self) -> dict:
        """Delete the jobs history and the worker logs; resetting the job counter.

        Rows are removed outright (the Library is directory-based, so entries
        there are unaffected) and sqlite_sequence is reset so the next job is #1.
        """
        with self.db_lock:
            active = self.db.execute(
                "SELECT id, status FROM jobs WHERE deleted=0 AND status IN ('pending','running')"
                " ORDER BY id ASC LIMIT 1").fetchone()
            if active is not None:
                raise ValueError(f"cannot clear history while job #{active[0]} is {active[1]}; "
                                 "cancel it first")
            cursor = self.db.execute("DELETE FROM jobs WHERE deleted=0")
            self.db.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")
            self.db.commit()
        logs = []
        logs_dir = self.settings.logs_dir
        if logs_dir.is_dir():
            for log_file in sorted(logs_dir.glob("*.log")):
                try:
                    log_file.unlink()
                    logs.append(log_file.name)
                except OSError:
                    # a pump write may hold the file briefly; retry once
                    time.sleep(0.05)
                    try:
                        log_file.unlink()
                        logs.append(log_file.name)
                    except OSError:
                        continue
        return {"cleared": cursor.rowcount, "logs": logs}

    # -- submission ----------------------------------------------------------
    def submit(self, kind: str, params: dict, name: str | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"Unknown job kind: {kind}")
        now = time.time()
        with self.db_lock:
            cursor = self.db.execute(
                "INSERT INTO jobs (kind, name, status, params, output_dir, created_at) VALUES (?,?,?,?,?,?)",
                (kind, name, "pending", json.dumps(params, default=str),
                 params.get("output_dir"), now))
            self.db.commit()
            job_id = cursor.lastrowid
        with self.wake:
            self.wake.notify_all()
        return self.get(job_id)

    # -- queue loop ----------------------------------------------------------
    def _loop(self) -> None:
        while True:
            job = None
            while job is None:
                job = self._next_pending()
                if job is not None:
                    break
                with self.wake:
                    self.wake.wait(timeout=5.0)
            self._execute(job)

    def _next_pending(self) -> dict | None:
        with self.db_lock:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status='pending' AND deleted=0 ORDER BY id ASC LIMIT 1").fetchone()
        return self._row(row)

    def _build(self, kind: str, params: dict) -> tuple[dict, dict]:
        yue2_spec = {"kind": "yue2", "model": self.settings.yue2_model, "vae": self.settings.yue2_vae,
                     "device": self.settings.yue2_device, "budget": self.settings.memory_budget_gib,
                     "backend": self.settings.yue2_backend, "quantization": self.settings.yue2_quantization,
                     "offload_ar": self.settings.yue2_offload_ar, "offline": self.settings.offline}
        if kind in ("generate", "plan"):
            request = {key: params[key] for key in ("style", "lyrics", "cot", "seed", "cfg_scale", "abc")
                       if params.get(key) is not None}
            request["id"] = params.get("slug", "song")
            command = {"cmd": "run", "action": kind, "job_id": None, "output_dir": params["output_dir"],
                       "request": request}
            if kind == "plan":
                command["sampling"] = {"abc": (params.get("sampling") or {}).get("abc")}
            else:
                command["sampling"] = params.get("sampling") or {}
            return yue2_spec, command
        if kind == "decode":
            source_dir = Path(self.settings.outputs_dir) / params["source"]
            if not (source_dir / "result.json").is_file():
                raise ValueError(f"Library song {params['source']!r} has no completed result to decode")
            vae = self.settings.yue2_vae_legacy if params["vae"] == "legacy" else "standard"
            command = {"cmd": "run", "action": "decode", "job_id": None,
                       "output_dir": params["output_dir"], "source_dir": str(source_dir),
                       "vae": vae, "offline": self.settings.offline}
            return yue2_spec, command
        audio = Path(self.settings.uploads_dir) / params["audio"]
        if not audio.is_file():
            raise FileNotFoundError(f"Uploaded audio {params['audio']!r} is missing")
        command = {"cmd": "run", "action": "transcribe", "job_id": None,
                   "output_dir": params["output_dir"], "audio": str(audio),
                   "task": params["task"], "preset": params["preset"], "max_seconds": params["max_seconds"]}
        sheetsage_spec = {"kind": "sheetsage", "model": self.settings.sheetsage2_model,
                          "offline": self.settings.offline, "device": self.settings.sheetsage2_device,
                          "dtype": self.settings.sheetsage2_dtype,
                          "abc_tools": str(self.settings.resolved_abc_tools_path())}
        return sheetsage_spec, command

    def _execute(self, job: dict) -> None:
        job_id, kind = job["id"], job["kind"]
        self._update(job_id, status="running", started_at=time.time())
        params = job["params"]
        try:
            spec, command = self._build(kind, params)
        except Exception as exc:
            self._update(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                         finished_at=time.time())
            return
        command["job_id"] = job_id
        worker = self.sheetsage if kind == "transcribe" else self.yue2
        other = self.yue2 if kind == "transcribe" else self.sheetsage
        try:
            if self.settings.residency == "on-demand":
                other.release()
            worker.ensure(spec)
            result = worker.run(job_id, command)
        except JobCancelled:
            self._finish(job_id, status="cancelled")
            return
        except Exception as exc:
            self._finish(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            return
        self._finish(job_id, status="done", result=result if isinstance(result, dict) else {"value": result})

    def _finish(self, job_id: int, status: str, **fields) -> dict | None:
        self._clear_cancel_timer(job_id)
        current = self.get(job_id)
        if current is not None and current["status"] in TERMINAL:
            return current
        fields.setdefault("finished_at", time.time())
        return self._update(job_id, status=status, **fields)

    # -- cancellation --------------------------------------------------------
    def cancel(self, job_id: int) -> dict:
        job = self.get(job_id)
        if job is None or job["deleted"]:
            raise KeyError(f"no such job: {job_id}")
        status = job["status"]
        if status == "pending":
            return self._finish(job_id, status="cancelled")
        if status != "running":
            raise ValueError(f"job {job_id} already finished ({status})")
        worker = self.sheetsage if job["kind"] == "transcribe" else self.yue2
        worker.request_cancel()
        timer = threading.Timer(180.0, self._force_cancel, (job_id,))
        timer.daemon = True
        with self.db_lock:
            self.cancel_timers[job_id] = timer
        timer.start()
        return self.get(job_id)

    def _force_cancel(self, job_id: int) -> None:
        job = self.get(job_id)
        if job is None or job["status"] != "running":
            return
        self._update(job_id, status="cancelled", error="cancelled (forced: the worker was stopped)",
                     finished_at=time.time())
        worker = self.sheetsage if job["kind"] == "transcribe" else self.yue2
        worker.kill()
        self._clear_cancel_timer(job_id)

    def _clear_cancel_timer(self, job_id: int) -> None:
        with self.db_lock:
            timer = self.cancel_timers.pop(job_id, None)
        if timer is not None:
            timer.cancel()

    # -- residency watchdog --------------------------------------------------
    def _watchdog(self) -> None:
        while True:
            time.sleep(30.0)
            try:
                if self.settings.residency != "on-demand":
                    continue
                idle = self.settings.release_idle_minutes * 60.0
                for worker in (self.yue2, self.sheetsage):
                    with worker.state_lock:
                        idle_for = worker.last_activity
                        ready = worker.model_state == "ready" and worker.busy == 0
                    if ready and idle_for is not None and time.time() - idle_for > idle:
                        worker.release()
            except Exception:
                continue

    def workers_snapshot(self) -> list[dict]:
        return [self.yue2.snapshot(), self.sheetsage.snapshot()]

    def shutdown(self) -> None:
        try:
            self.yue2.shutdown()
            self.sheetsage.shutdown()
        except Exception:
            pass
