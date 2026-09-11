"""In-process ABC services backed by the official abc_tools module (no model load)."""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()
_CACHE: dict[str, object] = {}


def _load_abc_tools(path: Path):
    path = Path(path)
    key = str(path)
    module = _CACHE.get(key)
    if module is not None:
        return module
    if not path.is_file():
        raise FileNotFoundError(
            f"abc_tools.py not found at {path}; point yue2.dir (config.yaml) at the YuE repository checkout")
    with _LOCK:
        module = _CACHE.get(key)
        if module is None:
            spec = importlib.util.spec_from_file_location("ds_yue_webui_abc_tools", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load ABC tools from {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            _CACHE[key] = module
    return module


def _abc_tools_module(settings):
    path = getattr(settings, "resolved_abc_tools_path", None)
    module_path = path() if callable(path) else Path(settings.abc_tools_path)
    return _load_abc_tools(module_path)


def inspect_abc(settings, text: str) -> dict:
    tools = _abc_tools_module(settings)
    try:
        score = tools.parse_abc(text)
        report = tools.report(score)
        return {"ok": True, "report": _jsonable(report),
                "has_chords": any(bool(report["voices"][name]["chords"]) for name in report["voices"])}
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "has_chords": None}


def strip_chords(settings, text: str, keep_voice: str = "both") -> dict:
    tools = _abc_tools_module(settings)
    if keep_voice not in ("both", "Vocal", "Ins"):
        raise ValueError(f"keep_voice must be both, Vocal or Ins, got {keep_voice!r}")
    output = tools.strip_chords(text, keep_voice)
    return {"ok": True, "abc": output, "kept_voice": keep_voice}


def compare_abc(settings, before: str, after: str, *, allow_tempo_change: bool = False,
                voices: str = "both") -> dict:
    tools = _abc_tools_module(settings)
    if voices not in ("both", "Vocal", "Ins"):
        raise ValueError(f"voices must be both, Vocal or Ins, got {voices!r}")
    names = tools.VOICES if voices == "both" else (voices,)
    try:
        result = tools.compare(tools.parse_abc(before), tools.parse_abc(after), names, allow_tempo_change)
        return {"ok": True, "compare": _jsonable(result)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def _jsonable(report: dict) -> dict:
    """abc_tools reports contain Fractions and tuples; make them JSON-safe in one pass."""
    from fractions import Fraction

    def convert(value):
        if isinstance(value, Fraction):
            return str(value)
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value

    return convert(report)
