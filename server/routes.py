"""REST API routes for the YuE2 web UI."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import abc_service, library
from .config import Settings
from .jobs import TERMINAL
from .validation import (ValidationError, slugify, unique_directory, unique_file,
                         validate_decode, validate_generate, validate_plan, validate_transcribe)


class JobRequest(BaseModel):
    kind: str = Field(pattern="^(generate|plan|transcribe|decode)$")
    name: str | None = None
    params: dict = Field(default_factory=dict)


class AbcInspectRequest(BaseModel):
    text: str


class AbcCompareRequest(BaseModel):
    before: str
    after: str
    allow_tempo_change: bool = False
    voices: str = Field(default="both", pattern="^(both|Vocal|Ins)$")


class AbcStripRequest(BaseModel):
    text: str
    keep_voice: str = Field(default="both", pattern="^(both|Vocal|Ins)$")


class ConfigUpdateRequest(BaseModel):
    residency: str | None = Field(default=None, pattern="^(on-demand|always)$")
    release_idle_minutes: float | None = Field(default=None, ge=0)


def create_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api")

    def queue(request: Request):
        return request.app.state.queue

    # -- config --------------------------------------------------------------
    @router.get("/config")
    def get_config(request: Request):
        snapshot = settings.snapshot()
        snapshot["modes"] = {"cot": ("full", "melody", "off"),
                             "transcribe_tasks": ("full", "melody-full", "melody-vocal"),
                             "residency": ("on-demand", "always"),
                             "vae_choices": ("standard", "legacy")}
        example_path = settings.yue2_dir / "examples" / "song.json"
        if example_path.is_file():
            try:
                snapshot["example_request"] = json.loads(example_path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return snapshot

    @router.post("/config")
    def update_config(request: Request, body: ConfigUpdateRequest):
        fields = {key: value for key, value in body.model_dump().items() if value is not None}
        if not fields:
            raise HTTPException(status_code=400, detail="nothing to update")
        try:
            settings.update_runtime(**fields)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"updated": sorted(fields), "config": settings.snapshot()}

    # -- jobs ----------------------------------------------------------------
    @router.post("/jobs")
    def submit_job(request: Request, body: JobRequest):
        params = dict(body.params)
        try:
            warnings: list[str] = []
            kind = body.kind
            if kind in ("generate", "plan"):
                cleaned, warnings = (validate_generate if kind == "generate" else validate_plan)(settings, params)
                slug = slugify(body.name)
                base = settings.outputs_dir if kind == "generate" else settings.plans_dir
                output_dir = unique_directory(base, slug)
                params.update(cleaned)
                params.update({"name": body.name, "slug": slug, "output_dir": str(output_dir)})
            elif kind == "transcribe":
                cleaned, warnings = validate_transcribe(params)
                audio = settings.uploads_dir / cleaned["audio"]
                if not audio.is_file():
                    raise ValidationError(f"Uploaded audio {cleaned['audio']!r} is missing")
                slug = slugify(body.name or Path(cleaned["audio"]).stem)
                output_dir = unique_directory(settings.transcripts_dir, slug)
                params.update(cleaned)
                params.update({"name": body.name, "slug": slug, "output_dir": str(output_dir)})
            else:
                cleaned, warnings = validate_decode(params)
                source_dir = settings.outputs_dir / cleaned["source"]
                if not (source_dir / "result.json").is_file():
                    raise ValidationError(f"Song {cleaned['source']!r} has no completed result to decode")
                slug = slugify(f"{cleaned['source']}-{cleaned['vae']}")
                output_dir = unique_directory(settings.outputs_dir, slug)
                params.update(cleaned)
                params.update({"slug": slug, "output_dir": str(output_dir)})
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            job = queue(request).submit(body.kind, params, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job": job, "warnings": warnings}

    @router.get("/jobs")
    def list_jobs(request: Request, limit: int = 50):
        return {"jobs": queue(request).list(max(1, min(limit, 500))),
                "counts": queue(request).counts()}

    @router.get("/jobs/{job_id}")
    def get_job(request: Request, job_id: int):
        job = queue(request).get(job_id)
        if job is None or job["deleted"]:
            raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
        return job

    @router.delete("/jobs")
    def clear_jobs(request: Request):
        try:
            return queue(request).clear()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/jobs/{job_id}")
    def cancel_job(request: Request, job_id: int):
        try:
            return queue(request).cancel(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/jobs/{job_id}/events")
    async def job_events(request: Request, job_id: int):
        job_queue = queue(request)

        async def stream():
            last_payload = None
            while True:
                job = await run_in_threadpool(job_queue.get, job_id)
                if job is None or job["deleted"]:
                    yield f"data: {json.dumps({'missing': True})}\n\n"
                    return
                payload = json.dumps(job, default=str)
                if payload != last_payload:
                    last_payload = payload
                    yield f"data: {payload}\n\n"
                if job["status"] in TERMINAL:
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- ABC services --------------------------------------------------------
    @router.post("/abc/inspect")
    def inspect_abc(body: AbcInspectRequest):
        return abc_service.inspect_abc(settings, body.text)

    @router.post("/abc/compare")
    def compare_abc(body: AbcCompareRequest):
        return abc_service.compare_abc(settings, body.before, body.after,
                                       allow_tempo_change=body.allow_tempo_change,
                                       voices=body.voices)

    @router.post("/abc/strip")
    def strip_abc(body: AbcStripRequest):
        try:
            return abc_service.strip_chords(settings, body.text, body.keep_voice)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"ABC check failed: {exc}") from exc

    # -- library -------------------------------------------------------------
    @router.get("/library")
    def get_library():
        return {"songs": library.list_directory_items(settings.outputs_dir, "song"),
                "transcripts": library.list_directory_items(settings.transcripts_dir, "transcript"),
                "plans": library.list_directory_items(settings.plans_dir, "plan")}

    @router.get("/library/songs/{name}")
    def get_song(name: str):
        try:
            return library.read_song(settings, name)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/library/songs/{name}/audio")
    def get_song_audio(name: str, format: str = "flac"):
        fmt = format.lower()
        if fmt not in ("flac", "mp3"):
            raise HTTPException(status_code=400, detail="format must be flac or mp3")
        try:
            if fmt == "mp3":
                path = library.mp3_path(settings, name)
            else:
                path = library.song_audio_path(settings, name)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return FileResponse(path, media_type="audio/mpeg" if fmt == "mp3" else "audio/flac",
                            filename=path.name)

    @router.get("/library/songs/{name}/artifacts/{relpath:path}")
    def get_song_artifact(name: str, relpath: str):
        try:
            path = library.song_artifact(settings, name, relpath)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(path)

    @router.delete("/library/songs/{name}")
    def delete_song(request: Request, name: str):
        try:
            library.delete_directory(settings, name, [settings.outputs_dir], queue(request))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"deleted": name}

    @router.get("/library/transcripts/{name}")
    def get_transcript(name: str):
        try:
            return library.read_transcript(settings, name)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/library/transcripts/{name}")
    def delete_transcript(request: Request, name: str):
        try:
            library.delete_directory(settings, name, [settings.transcripts_dir], queue(request))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"deleted": name}

    @router.get("/library/plans/{name}")
    def get_plan(name: str):
        try:
            return library.read_plan(settings, name)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.delete("/library/plans/{name}")
    def delete_plan(request: Request, name: str):
        try:
            library.delete_directory(settings, name, [settings.plans_dir], queue(request))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"deleted": name}

    # -- uploads -------------------------------------------------------------
    @router.post("/uploads")
    async def upload_audio(request: Request):
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" not in content_type:
            raise HTTPException(status_code=400, detail="upload with multipart/form-data")
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="multipart field 'file' is required")
        filename = Path(upload.filename or "audio").name
        if not filename or filename.startswith("."):
            raise HTTPException(status_code=400, detail="invalid upload filename")
        slug = slugify(Path(filename).stem)
        target = unique_file(settings.uploads_dir, slug, Path(filename).suffix or ".bin")
        total = 0
        with target.open("wb") as handle:
            while True:
                chunk = await upload.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                handle.write(chunk)
                if total > 2 * (1 << 30):
                    break
        if total < 1025:
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="audio upload is too small")
        if total > 2 * (1 << 30):
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=413, detail="audio upload exceeds the 2 GiB limit")
        return {"file": target.name, "path": str(target), "bytes": total,
                "dir": str(target.parent)}

    # -- diagnostics ---------------------------------------------------------
    @router.get("/diagnostics")
    def get_diagnostics(request: Request):
        from .diagnostics import build_report

        return build_report(settings, queue(request))

    return router
