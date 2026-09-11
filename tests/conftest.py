"""Shared fixtures: temp settings and a TestClient against mock workers."""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def settings(tmp_path):
    from server.config import Settings

    s = Settings.from_sources(tmp_path / "config.yaml", environ={})
    s.release_idle_minutes = 0.0
    s.yue2_model = "m-a-p/YuE2-3B"
    s.sheetsage2_model = "m-a-p/SheetSage2"
    s.ensure_dirs()
    return s


@pytest.fixture(scope="session")
def mock_worker_dir(tmp_path_factory):
    directory = tmp_path_factory.mktemp("mockworkers")
    body = (REPO_ROOT / "tests" / "mock_worker_body.py").read_text(encoding="utf-8")
    for name in ("yue2_worker.py", "sheetsage_worker.py"):
        (directory / name).write_text(body, encoding="utf-8")
    return directory


def _point_at_mocks(queue, mock_worker_dir: Path) -> None:
    interpreter = Path(sys.executable)
    for worker in (queue.yue2, queue.sheetsage):
        worker.python = interpreter
        worker.script = mock_worker_dir / worker.script.name


@pytest.fixture()
def queue(settings, mock_worker_dir):
    from server.jobs import JobQueue

    q = JobQueue(settings)
    _point_at_mocks(q, mock_worker_dir)
    try:
        yield q
    finally:
        q.shutdown()


@pytest.fixture()
def client(settings, mock_worker_dir):
    from fastapi.testclient import TestClient

    from server.app import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        _point_at_mocks(app.state.queue, mock_worker_dir)
        yield test_client


def wait_for_job(queue, job_id: int, timeout: float = 30.0, status: str = "done"):
    import time

    deadline = time.time() + timeout
    job = queue.get(job_id)
    while job is not None and job["status"] not in ("done", "failed", "cancelled"):
        if time.time() > deadline:
            raise AssertionError(f"job {job_id} did not finish within {timeout}s: {job}")
        time.sleep(0.1)
        job = queue.get(job_id)
    assert job is not None
    if status and job["status"] != status:
        raise AssertionError(f"job {job_id} finished as {job['status']}: {job.get('error')}")
    return job


def load_sample_score(settings) -> str:
    path = REPO_ROOT / "tests" / "samples" / "score.abc"
    return path.read_text(encoding="utf-8")


def load_sample_jazz(settings) -> str:
    path = REPO_ROOT / "tests" / "samples" / "score-jazz.abc"
    return path.read_text(encoding="utf-8")
