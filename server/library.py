"""Library operations over native YuE2 song result dirs, transcriptions, and plans."""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}")


def unique_directory(base_dir: Path, slug: str) -> Path:
    """Create a fresh timestamped output directory for a new library item."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = base_dir / f"{slug}-{stamp}"
    candidate, index = base, 1
    while candidate.exists():
        index += 1
        candidate = base_dir / f"{base.name}-{index}"
    candidate.mkdir(parents=True)
    return candidate


def _locate(candidates: list[Path], name: str) -> Path | None:
    if not ID_PATTERN.fullmatch(name or ""):
        raise ValueError("invalid name")
    for root in candidates:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def locate_song(settings, name: str) -> Path:
    found = _locate([settings.outputs_dir], name)
    if found is None:
        raise FileNotFoundError(f"no such song: {name}")
    return found


def locate_transcript(settings, name: str) -> Path:
    found = _locate([settings.transcripts_dir], name)
    if found is None:
        raise FileNotFoundError(f"no such transcript: {name}")
    return found


def locate_plan(settings, name: str) -> Path:
    found = _locate([settings.plans_dir], name)
    if found is None:
        raise FileNotFoundError(f"no such plan: {name}")
    return found


def _read_json(path: Path):
    import json

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # the YuE2 runtime historically wrote json with the locale encoding
        # (cp1252 on Windows), producing files valid JSON cannot read as utf-8
        try:
            text = raw.decode("mbcs")
        except (LookupError, UnicodeDecodeError):
            text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text.lstrip("﻿"))
    except (OSError, ValueError):
        return None


def read_song(settings, name: str) -> dict:
    directory = locate_song(settings, name)
    result = _read_json(directory / "result.json") or {}
    summary = {
        "name": directory.name,
        "dir": str(directory),
        "status": result.get("status", "incomplete"),
        "result": result,
        "request": _read_json(directory / "request.json"),
        "config": _read_json(directory / "config.json"),
        "truncated": result.get("truncated"),
        "audio_seconds": result.get("audio_seconds"),
        "decoder": result.get("decoder"),
        "sources": result.get("sources"),
        "has_audio": (directory / "audio.flac").is_file(),
        "abc": None,
    }
    abc_path = directory / "score.abc"
    if abc_path.is_file():
        summary["abc"] = abc_path.read_text(encoding="utf-8", errors="replace")
    return summary


def read_transcript(settings, name: str) -> dict:
    directory = locate_transcript(settings, name)
    manifest = _read_json(directory / "transcription_manifest.json") or {}
    abc_check = _read_json(directory / "abc_check.json")
    summary = {
        "name": directory.name,
        "dir": str(directory),
        "status": manifest.get("status", "incomplete"),
        "manifest": manifest,
        "warnings": manifest.get("warnings", []),
        "abc_check": abc_check,
        "abc": None,
        "has_audio_manifest": _read_json(directory / "input.json"),
    }
    abc_path = directory / "score.abc"
    if abc_path.is_file():
        summary["abc"] = abc_path.read_text(encoding="utf-8", errors="replace")
    return summary


def read_plan(settings, name: str) -> dict:
    directory = locate_plan(settings, name)
    plan = _read_json(directory / "plan.json") or {}
    summary = {
        "name": directory.name,
        "dir": str(directory),
        "status": "complete" if (directory / "plan_manifest.json").is_file() else "incomplete",
        "request": plan.get("request"),
        "truncated": plan.get("truncated"),
        "timing": plan.get("timing"),
        "abc": None,
    }
    abc_path = directory / "score.abc"
    if abc_path.is_file():
        summary["abc"] = abc_path.read_text(encoding="utf-8", errors="replace")
    return summary


def song_audio_path(settings, name: str) -> Path:
    directory = locate_song(settings, name)
    audio = directory / "audio.flac"
    if not audio.is_file():
        raise FileNotFoundError(f"song {name} has no audio.flac")
    return audio


def song_artifact(settings, name: str, relpath: str) -> Path:
    directory = locate_song(settings, name)
    parts = [part for part in Path(relpath).parts if part not in ("..",)]
    if not parts or any(part.startswith(".") and part not in {".abc", ".json", ".npy", ".flac"} for part in parts):
        raise ValueError("invalid artifact path")
    candidate = directory.joinpath(*parts)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(directory.resolve()):
        raise ValueError("invalid artifact path")
    if not candidate.is_file():
        raise FileNotFoundError(relpath)
    return candidate


def mp3_path(settings, name: str) -> Path:
    """Convert audio.flac to mp3 on demand (browser-friendly delivery) with a cache."""
    source = song_audio_path(settings, name)
    cache = settings.cache_dir / "audio"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{Path(name).name}.mp3"
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for mp3 delivery; install it and add it to PATH")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-codec:a", "libmp3lame", "-q:a", "2", str(target)],
        capture_output=True, timeout=600, check=False)
    if result.returncode or not target.is_file():
        raise RuntimeError("mp3 conversion failed: " + result.stderr.decode(errors="replace")[-400:])
    return target


def list_directory_items(directory: Path, kind: str) -> list[dict]:
    """Scan a directory for library entries, independent of the jobs history.

    New entries appear as soon as their files exist on disk, and they survive
    clearing the jobs history. Entries are newest-first by directory mtime.
    """
    entries = []
    if not directory.is_dir():
        return entries
    for child in directory.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            entry = {"name": child.name, "output_dir": str(child),
                     "created_at": child.stat().st_mtime}
            if kind == "song":
                result = _read_json(child / "result.json") or {}
                entry["status"] = ("done" if result.get("status") == "complete"
                                   else "failed" if result else "incomplete")
                entry["audio_seconds"] = result.get("audio_seconds")
                entry["has_audio"] = (child / "audio.flac").is_file()
                entry["audio"] = entry["has_audio"]
            elif kind == "transcript":
                manifest = _read_json(child / "transcription_manifest.json") or {}
                entry["status"] = "done" if manifest.get("status") == "complete" else "incomplete"
            else:
                plan = _read_json(child / "plan.json") or {}
                entry["status"] = "complete" if (child / "plan_manifest.json").is_file() else "incomplete"
                entry["timing"] = plan.get("timing")
                entry["truncated"] = plan.get("truncated")
            entries.append(entry)
        except OSError:
            continue
    entries.sort(key=lambda item: item["created_at"], reverse=True)
    return entries


def delete_directory(settings, name: str, directories: list[Path], job_queue=None) -> None:
    """Delete a Library item's files (plus its cached mp3). The jobs history is
    left untouched: entries stay, IDs stay contiguous, and old entries keep
    their recorded data — deleting an artifact does not erase the audit trail."""
    directory = _locate(directories, name)
    if directory is None:
        raise FileNotFoundError(f"no such item: {name}")
    if job_queue is not None:
        try:
            active = next((entry for entry in job_queue.list(500)
                           if (entry.get("output_dir") or "") == str(directory)
                           and entry.get("status") in ("pending", "running")), None)
        except Exception:
            active = None
        if active is not None:
            raise ValueError(f"an active job is writing to this directory "
                             f"(job #{active['id']}); cancel it first")
    shutil.rmtree(directory)
    for cache_item in (settings.cache_dir / "audio" / f"{name}.mp3",):
        if cache_item.is_file():
            cache_item.unlink()
