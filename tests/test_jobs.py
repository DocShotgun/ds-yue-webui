"""Job queue tests with mock workers (GPU-free, exercises the full NDJSON pipeline)."""
import json
import sqlite3

from server.jobs import JobQueue
from server.validation import slugify, unique_directory

from conftest import wait_for_job


def make_params(kind, tmp_path):
    slug = slugify("test song")
    base = tmp_path / "data" / ("outputs" if kind in ("generate", "decode") else "plans" if kind == "plan" else "transcripts")
    output_dir = unique_directory(base, slug)
    if kind in ("generate", "plan"):
        return {"style": "English, warm piano pop", "lyrics": "[Verse]\nhello", "cot": "full",
                "slug": slug, "output_dir": str(output_dir)}, output_dir
    if kind == "transcribe":
        uploads = tmp_path / "data" / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        (uploads / "clip.wav").write_bytes(b"\x01" * 2048)
        return {"audio": "clip.wav", "task": "full", "preset": "default", "max_seconds": None,
                "slug": slug, "output_dir": str(output_dir)}, output_dir
    return {"source": "source_song", "vae": "legacy", "slug": slug,
            "output_dir": str(unique_directory(tmp_path / "data" / "outputs", slug))}, output_dir


def run_job(queue, kind, tmp_path):
    params, output_dir = make_params(kind, tmp_path)
    job = queue.submit(kind, params, "test song")
    return wait_for_job(queue, job["id"]), output_dir


def test_generate_job_end_to_end(queue, tmp_path):
    job, output_dir = run_job(queue, "generate", tmp_path)
    assert job["status"] == "done"
    assert (output_dir / "result.json").is_file()
    result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "complete"
    assert job["result"]["audio_seconds"] == 10.0
    assert job["progress"] is not None  # progress events were recorded


def test_plan_and_transcribe_jobs(queue, tmp_path):
    job, output_dir = run_job(queue, "plan", tmp_path)
    assert job["status"] == "done"
    assert (output_dir / "plan.json").is_file()
    job, output_dir = run_job(queue, "transcribe", tmp_path)
    assert job["status"] == "done"
    assert (output_dir / "score.abc").is_file()


def test_pending_cancel(queue, tmp_path):
    params, _ = make_params("generate", tmp_path)
    first = queue.submit("generate", params, "a")
    second = queue.submit("generate", {**params, "slug": "b"}, "b")
    cancelled = queue.cancel(second["id"])
    assert cancelled["status"] == "cancelled"
    # the first job still finishes
    first_done = wait_for_job(queue, first["id"])
    assert first_done["status"] == "done"


def test_serialization_one_at_a_time(queue, tmp_path):
    params, _ = make_params("generate", tmp_path)
    jobs = [queue.submit("generate", {**params, "slug": f"s{i}"}, f"s{i}") for i in range(3)]
    for job in jobs:
        wait_for_job(queue, job["id"])
    counts = queue.counts()
    assert counts.get("done") == 3
    assert counts.get("running", 0) in (0, None)


def test_decode_job(queue, tmp_path):
    run_job(queue, "generate", tmp_path)  # creates a "test-song-*" song
    source = next((path for path in (tmp_path / "data" / "outputs").iterdir() if path.is_dir()), None)
    assert source is not None
    params = {"source": source.name, "vae": "legacy",
              "slug": slugify("decode"), "output_dir": str(unique_directory(tmp_path / "outputs", "decode"))}
    job = queue.submit("decode", params, "decode")
    done = wait_for_job(queue, job["id"])
    assert done["status"] == "done"


def test_job_recovery_on_restart(settings, tmp_path):
    """A pending/running job in the DB is marked failed when the server restarts."""
    params, _ = make_params("generate", tmp_path)
    db_path = settings.data_dir / "jobs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), check_same_thread=False)
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, name TEXT, status TEXT DEFAULT 'pending',
        params TEXT, output_dir TEXT, error TEXT, progress TEXT, result TEXT,
        deleted INTEGER NOT NULL DEFAULT 0, created_at REAL, started_at REAL, finished_at REAL)""")
    db.execute("INSERT INTO jobs (kind, name, status, params, created_at) VALUES ('generate','x','pending','{}',1)")
    db.execute("INSERT INTO jobs (kind, name, status, params, created_at) VALUES ('generate','y','running','{}',2)")
    db.commit()
    db.close()
    q = JobQueue(settings)
    try:
        done = q.list(10)
        assert len(done) == 2
        assert all(job["status"] == "failed" for job in done)
        assert "interrupted" in done[0]["error"]
    finally:
        q.shutdown()


def test_clear_history(queue, tmp_path):
    import pytest

    params, _ = make_params("generate", tmp_path)
    job = queue.submit("generate", {**params, "slug": "clear-test"}, "clear test")
    with queue.db_lock:
        queue.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    with pytest.raises(ValueError):
        queue.clear()
    wait_for_job(queue, job["id"])
    logs_dir = queue.settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "worker-yue2.log").write_text("boot\n", encoding="utf-8")
    (logs_dir / "worker-sheetsage.log").write_text("boot\n", encoding="utf-8")
    cleared = queue.clear()
    assert cleared["cleared"] >= 1
    assert sorted(cleared["logs"]) == ["worker-sheetsage.log", "worker-yue2.log"]
    assert queue.list() == []
    assert queue.counts() == {}
    queue.clear()  # idempotent on an empty history
    assert not any(logs_dir.glob("*.log"))
    followup = queue.submit("generate", {**params, "slug": "after-clear"}, "after clear")
    assert followup["id"] == 1, "the job counter resets after clearing"
