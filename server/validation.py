"""Fail-fast request validation mirroring the YuE2 runtime's SongRequest and Sampling rules."""
from __future__ import annotations

import math
import re

from .config import Settings


class ValidationError(ValueError):
    """Rejected client input."""


ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}")
COT_MODES = ("full", "melody", "off")
SAMPLING_INT_KEYS = ("top_k", "penalty_window", "min_tokens", "max_tokens")
SAMPLING_FLOAT_KEYS = ("temperature", "top_p", "repetition_penalty")


def slugify(name: str | None) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", (name or "").strip()).strip("-.")
    return text[:120] if text else "song"


def unique_directory(base_dir, slug: str):
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = base_dir / f"{slug}-{stamp}"
    candidate, index = base, 1
    while candidate.exists():
        index += 1
        candidate = base_dir / f"{base.name}-{index}"
    candidate.mkdir(parents=True)
    return candidate


def _clean_sampling(section: dict | None, label: str) -> dict | None:
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValidationError(f"sampling.{label} must be an object")
    unknown = set(section) - set(SAMPLING_INT_KEYS) - set(SAMPLING_FLOAT_KEYS)
    if unknown:
        raise ValidationError(f"Unknown sampling.{label} fields: {sorted(unknown)}")
    cleaned = {}
    for key in SAMPLING_INT_KEYS:
        if key in section and section[key] is not None:
            if not isinstance(section[key], int) or isinstance(section[key], bool):
                raise ValidationError(f"sampling.{label}.{key} must be an integer")
            cleaned[key] = section[key]
    for key in SAMPLING_FLOAT_KEYS:
        if key in section and section[key] is not None:
            value = section[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValidationError(f"sampling.{label}.{key} must be a finite number")
            cleaned[key] = value
    temperature = cleaned.get("temperature", 1.0)
    top_p = cleaned.get("top_p", 0.95)
    top_k = cleaned.get("top_k", 100)
    repetition_penalty = cleaned.get("repetition_penalty", 1.2)
    penalty_window = cleaned.get("penalty_window", 50)
    min_tokens = cleaned.get("min_tokens", 200)
    max_tokens = cleaned.get("max_tokens", 9000)
    if not 0 <= temperature <= 5 or not 0 < top_p <= 1 or top_k < 1:
        raise ValidationError(f"Invalid sampling.{label} temperature/top_p/top_k")
    if repetition_penalty <= 0 or not 1 <= penalty_window <= 100:
        raise ValidationError(f"Invalid sampling.{label} repetition penalty/window")
    if not 0 <= min_tokens <= max_tokens or max_tokens < 1:
        raise ValidationError(f"Require 0 <= sampling.{label} min_tokens <= max_tokens")
    return cleaned or None


def _check_cot(cot) -> str:
    if cot is None:
        return "full"
    if cot not in COT_MODES:
        raise ValidationError(f"cot must be full, melody or off, got {cot!r}")
    return cot


def _check_seed(seed) -> int | None:
    if seed is None:
        return None
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**63:
        raise ValidationError("seed must be an integer in [0, 2**63)")
    return seed


def _check_cfg(cfg_scale) -> float | None:
    if cfg_scale is None:
        return None
    if isinstance(cfg_scale, bool) or not isinstance(cfg_scale, (int, float)) or not math.isfinite(cfg_scale) \
            or not 0 <= cfg_scale <= 20:
        raise ValidationError("cfg_scale must be finite and in [0, 20]")
    return float(cfg_scale)


def _request_common(params: dict, *, allow_off: bool) -> tuple[dict, list[str]]:
    if not isinstance(params, dict):
        raise ValidationError("Request body must be an object")
    unknown = set(params) - {"name", "style", "lyrics", "cot", "seed", "cfg_scale", "abc", "sampling"}
    if unknown:
        raise ValidationError(f"Unknown request fields: {sorted(unknown)}")
    warnings: list[str] = []
    style = params.get("style")
    if not isinstance(style, str) or not style.strip():
        raise ValidationError("style is required (genre, instruments, vocal, language, tempo)")
    if len(style) > 5000:
        raise ValidationError("style is too long (max 5000 characters)")
    lyrics = params.get("lyrics")
    if not isinstance(lyrics, str):
        raise ValidationError("lyrics must be a string")
    if not lyrics.strip():
        warnings.append("lyrics are empty; the song will be instrumental but the runtime still expects lyrics text")
    elif len(lyrics) > 60000:
        raise ValidationError("lyrics are too long (max 60000 characters)")
    elif len(lyrics) > 10000:
        warnings.append("lyrics are long; the model context is finite and may truncate the plan or song")
    cot = _check_cot(params.get("cot"))
    if cot == "off" and not allow_off:
        raise ValidationError("cot=off is not valid for this workflow")
    abc = params.get("abc")
    if abc is not None:
        if not isinstance(abc, str) or not abc.strip():
            raise ValidationError("abc must be nonempty score text")
        if cot == "off":
            raise ValidationError("External ABC requires cot=melody/full, not off")
    return {"style": style, "lyrics": lyrics, "cot": cot,
            "seed": _check_seed(params.get("seed")),
            "cfg_scale": _check_cfg(params.get("cfg_scale")),
            "abc": abc,
            "sampling": {"abc": _clean_sampling((params.get("sampling") or {}).get("abc") if isinstance(params.get("sampling"), dict) else None, "abc"),
                         "semantic": _clean_sampling((params.get("sampling") or {}).get("semantic") if isinstance(params.get("sampling"), dict) else None, "semantic")}}, warnings


def _check_abc_chords(settings: Settings, abc: str, warnings: list[str]) -> None:
    """Melody-mode input must not carry chord symbols (mirrors the official skill script)."""
    from . import abc_service

    try:
        result = abc_service.inspect_abc(settings, abc)
    except FileNotFoundError as exc:
        warnings.append(f"ABC validation unavailable: {exc}")
        return
    if not result["ok"]:
        warnings.append(f"ABC dialect check failed ({result['error']}); the score is passed to the model as-is")
        return
    if result["has_chords"]:
        raise ValidationError(
            "melody-mode input must not contain chord symbols; use cot=full to keep them or strip chords first")


def validate_generate(settings: Settings, params: dict) -> tuple[dict, list[str]]:
    cleaned, warnings = _request_common(params, allow_off=True)
    if settings is not None and cleaned["abc"] is not None and cleaned["cot"] == "melody":
        _check_abc_chords(settings, cleaned["abc"], warnings)
    return cleaned, warnings


def validate_plan(settings: Settings, params: dict) -> tuple[dict, list[str]]:
    cleaned, warnings = _request_common(params, allow_off=False)
    if settings is not None and cleaned["abc"] is not None and cleaned["cot"] == "melody":
        _check_abc_chords(settings, cleaned["abc"], warnings)
    return cleaned, warnings


def validate_transcribe(params: dict) -> tuple[dict, list[str]]:
    if not isinstance(params, dict):
        raise ValidationError("Request body must be an object")
    unknown = set(params) - {"audio", "task", "preset", "max_seconds"}
    if unknown:
        raise ValidationError(f"Unknown request fields: {sorted(unknown)}")
    audio = params.get("audio")
    if not isinstance(audio, str) or not audio.strip():
        raise ValidationError("audio is required (an uploaded file name)")
    task = params.get("task") or "full"
    if task not in ("full", "melody-full", "melody-vocal"):
        raise ValidationError(f"task must be full, melody-full or melody-vocal, got {task!r}")
    preset = params.get("preset") or "default"
    if preset not in ("default", "paper"):
        raise ValidationError(f"preset must be default or paper, got {preset!r}")
    max_seconds = params.get("max_seconds")
    if max_seconds is not None:
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) \
                or not math.isfinite(max_seconds) or max_seconds <= 0:
            raise ValidationError("max_seconds must be a positive number")
        max_seconds = float(max_seconds)
    return {"audio": audio, "task": task, "preset": preset, "max_seconds": max_seconds}, []


def validate_decode(params: dict) -> tuple[dict, list[str]]:
    if not isinstance(params, dict):
        raise ValidationError("Request body must be an object")
    unknown = set(params) - {"source", "vae"}
    if unknown:
        raise ValidationError(f"Unknown request fields: {sorted(unknown)}")
    source = params.get("source")
    if not isinstance(source, str) or not ID_PATTERN.fullmatch(source):
        raise ValidationError("source must be a library song name")
    vae = params.get("vae") or "standard"
    if vae not in ("standard", "legacy"):
        raise ValidationError(f"vae must be standard or legacy, got {vae!r}")
    return {"source": source, "vae": vae}, []
