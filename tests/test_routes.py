"""API route tests against the TestClient with mock workers."""
import json
import time
from pathlib import Path

from conftest import load_sample_jazz, load_sample_score, wait_for_job


def _poll(client, job_id, timeout=30.0, status="done"):
    deadline = time.time() + timeout
    while True:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            assert job["status"] == status, job
            return job
        assert time.time() < deadline, f"job {job_id} did not finish"
        time.sleep(0.1)


def _name_of(output_dir):
    return output_dir.replace("\\", "/").split("/")[-1]


def test_config_endpoints(client, tmp_path):
    config = client.get("/api/config").json()
    assert config["residency"] in ("on-demand", "always")
    assert "modes" in config
    updated = client.post("/api/config", json={"residency": "always", "release_idle_minutes": 5}).json()
    assert updated["config"]["residency"] == "always"
    assert (tmp_path / "config.yaml").is_file()
    bad = client.post("/api/config", json={"residency": "never"})
    assert bad.status_code in (400, 422)


def test_generate_job_flow(client, settings):
    response = client.post("/api/jobs", json={
        "kind": "generate", "name": "test song",
        "params": {"style": "English, warm piano pop", "lyrics": "[Verse]\nhello\n[Chorus]\nagain",
                   "cot": "full", "seed": 831001}})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["warnings"] == []
    job = data["job"]
    name = job["params"]["slug"]
    output_dir = Path(job["output_dir"])
    assert output_dir.is_dir()
    done = _poll(client, job["id"])
    assert done["result"]["audio_seconds"] == 10.0
    assert (output_dir / "result.json").is_file()

    library = client.get("/api/library").json()
    names = [_name_of(entry["output_dir"]) for entry in library["songs"]]
    assert _name_of(job["output_dir"]) in names

    detail = client.get(f"/api/library/songs/{_name_of(job['output_dir'])}").json()
    assert detail["status"] == "complete"
    assert detail["abc"] is None  # mock worker does not write score.abc
    artifact = client.get(f"/api/library/songs/{_name_of(job['output_dir'])}/artifacts/result.json")
    assert artifact.status_code == 200


def test_generate_validation_errors(client, settings):
    sample_score = load_sample_score(settings)
    bad = client.post("/api/jobs", json={
        "kind": "generate", "params": {"style": "s", "lyrics": "l", "cot": "off", "abc": sample_score}})
    assert bad.status_code == 400
    bad = client.post("/api/jobs", json={
        "kind": "generate", "params": {"style": "s", "lyrics": "l", "cot": "melody", "abc": sample_score}})
    assert bad.status_code == 400
    assert "chord symbols" in bad.json()["detail"]
    bad = client.post("/api/jobs", json={
        "kind": "generate", "params": {"style": "s", "lyrics": "l", "bpm": 120}})
    assert bad.status_code == 400
    bad = client.post("/api/jobs", json={"kind": "generate", "params": {"lyrics": "l"}})
    assert bad.status_code == 400
    bad = client.post("/api/jobs", json={"kind": "remix", "params": {}})
    assert bad.status_code == 422


def test_solve_plan_endpoint_flow(client):
    response = client.post("/api/jobs", json={
        "kind": "plan", "name": "plan test",
        "params": {"style": "warm piano", "lyrics": "[Verse]\nLa"}})
    assert response.status_code == 200
    _poll(client, response.json()["job"]["id"])
    library = client.get("/api/library").json()
    assert library["plans"]


def test_clear_history_endpoint(client, settings):
    logs_dir = settings.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "worker-yue2.log").write_text("boot\n", encoding="utf-8")
    response = client.post("/api/jobs", json={
        "kind": "plan", "name": "clear test",
        "params": {"style": "warm piano", "lyrics": "[Verse]\nLa"}})
    assert response.status_code == 200
    _poll(client, response.json()["job"]["id"])
    assert client.get("/api/jobs").json()["jobs"]
    cleared = client.delete("/api/jobs")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["cleared"] >= 1
    assert cleared.json()["logs"]
    assert client.get("/api/jobs").json()["jobs"] == []
    again = client.delete("/api/jobs")
    assert again.status_code == 200
    assert again.json()["cleared"] == 0

    # the Library is directory-based: entries survive clearing the jobs history
    library = client.get("/api/library").json()
    assert library["plans"], "plan files should still be listed after clearing jobs"
    assert Path(library["plans"][0]["output_dir"]).parent.name == "plans"

    # the job counter resets: the next job is #1 again
    followup = client.post("/api/jobs", json={
        "kind": "plan", "name": "after clear",
        "params": {"style": "warm piano", "lyrics": "[Verse]\nLa"}})
    assert followup.status_code == 200
    assert followup.json()["job"]["id"] == 1
    _poll(client, 1)


def test_transcript_detail_has_abc(client):
    audio = b"\x01" * 4096
    upload = client.post("/api/uploads", files={"file": ("clip.wav", audio, "audio/wav")})
    assert upload.status_code == 200
    file_name = upload.json()["file"]
    response = client.post("/api/jobs", json={
        "kind": "transcribe", "name": "abc detail test",
        "params": {"audio": file_name, "task": "full", "preset": "default"}})
    assert response.status_code == 200, response.text
    _poll(client, response.json()["job"]["id"])
    library = client.get("/api/library").json()
    assert library["transcripts"]
    name = _name_of(library["transcripts"][0]["output_dir"])
    detail = client.get(f"/api/library/transcripts/{name}").json()
    assert detail["abc"], "score.abc must be surfaced for review"
    assert "[Verse]" in detail["abc"] or detail["abc"].strip()


def test_transcribe_flow(client):
    audio = b"\x01" * 4096
    upload = client.post("/api/uploads", files={"file": ("clip.wav", audio, "audio/wav")})
    assert upload.status_code == 200
    file_name = upload.json()["file"]
    too_small = client.post("/api/uploads", files={"file": ("tiny.wav", b"x", "audio/wav")})
    assert too_small.status_code == 400

    response = client.post("/api/jobs", json={
        "kind": "transcribe", "name": "cover test",
        "params": {"audio": file_name, "task": "full", "preset": "default"}})
    assert response.status_code == 200, response.text
    _poll(client, response.json()["job"]["id"])
    library = client.get("/api/library").json()
    assert library["transcripts"]
    name = _name_of(library["transcripts"][0]["output_dir"])
    detail = client.get(f"/api/library/transcripts/{name}").json()
    assert detail["status"] == "complete"


def test_decode_flow(client):
    gen = client.post("/api/jobs", json={
        "kind": "generate", "name": "decode source",
        "params": {"style": "warm piano", "lyrics": "[Verse]\nLa", "cot": "full"}})
    _poll(client, gen.json()["job"]["id"])
    library = client.get("/api/library").json()
    source = _name_of(library["songs"][0]["output_dir"])
    bad = client.post("/api/jobs", json={"kind": "decode", "params": {"source": "nope", "vae": "legacy"}})
    assert bad.status_code == 400
    response = client.post("/api/jobs", json={"kind": "decode", "params": {"source": source, "vae": "legacy"}})
    assert response.status_code == 200
    _poll(client, response.json()["job"]["id"])


def test_abc_endpoints(client, settings):
    sample_score = load_sample_score(settings)
    sample_jazz = load_sample_jazz(settings)
    inspect = client.post("/api/abc/inspect", json={"text": sample_score})
    assert inspect.status_code == 200
    assert inspect.json()["ok"] and inspect.json()["has_chords"] is True

    strip = client.post("/api/abc/strip", json={"text": sample_score, "keep_voice": "both"})
    assert strip.status_code == 200
    stripped = strip.json()["abc"]
    recheck = client.post("/api/abc/inspect", json={"text": stripped}).json()
    assert recheck["ok"] and recheck["has_chords"] is False

    compare = client.post("/api/abc/compare", json={"before": sample_score, "after": sample_jazz})
    assert compare.status_code == 200
    assert compare.json()["compare"]["match"] is True

    bad = client.post("/api/abc/strip", json={"text": "garbage"})
    assert bad.status_code == 400


def test_job_events_stream(client):
    response = client.post("/api/jobs", json={
        "kind": "generate", "name": "events",
        "params": {"style": "warm piano", "lyrics": "[Verse]\nLa", "cot": "full"}})
    job_id = response.json()["job"]["id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        saw_active = False
        for line in stream.iter_lines():
            if line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
                if payload.get("status") in ("running", "done"):
                    saw_active = True
                if payload.get("status") in ("done", "failed", "cancelled"):
                    break
        assert saw_active


def test_cancel_and_404(client):
    bad = client.get("/api/jobs/99999")
    assert bad.status_code == 404
    bad = client.get("/api/library/songs/does_not_exist")
    assert bad.status_code == 404
    bad = client.delete("/api/jobs/99999")
    assert bad.status_code == 404


def test_diagnostics(client):
    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    report = response.json()
    assert "config" in report and "queue" in report and "doctor" in report
    assert report["config"]["data_dir"]
    assert isinstance(report["doctor"], dict)
