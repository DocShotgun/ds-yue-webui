"""Request-validation tests mirroring the YuE2 runtime rules."""
import pytest

from conftest import load_sample_score
from server.validation import (ValidationError, slugify, validate_decode,
                               validate_generate, validate_plan, validate_transcribe)

GOOD = {"style": "English, warm piano pop", "lyrics": "[Verse]\nhello\n[Chorus]\nagain"}


def test_slugify():
    assert slugify("My Song!)") == "My-Song"
    assert slugify("  ") == "song"
    assert slugify(None) == "song"
    assert len(slugify("x" * 500)) <= 120


def test_generate_valid():
    cleaned, warnings = validate_generate(None, dict(GOOD))
    assert cleaned["cot"] == "full"
    assert cleaned["abc"] is None
    assert isinstance(warnings, list)


def test_generate_requires_style():
    with pytest.raises(ValidationError):
        validate_generate(None, {"lyrics": "x"})


def test_generate_empty_lyrics_warns():
    _, warnings = validate_generate(None, {"style": "piano pop", "lyrics": ""})
    assert any("lyrics are empty" in warning for warning in warnings)


def test_generate_cot_off_with_abc_rejected():
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "cot": "off", "abc": "X:1"})


def test_generate_melody_with_chords_rejected(settings):
    # score.abc (native dialect) carries chord symbols in the Vocal voice
    with pytest.raises(ValidationError, match="chord symbols"):
        validate_generate(settings, {**GOOD, "cot": "melody", "abc": load_sample_score(settings)})


def test_generate_melody_with_ungarbage_abc_warns(settings):
    _, warnings = validate_generate(settings, {**GOOD, "cot": "melody", "abc": "not an abc score at all"})
    assert any("dialect check failed" in warning for warning in warnings)


def test_generate_seed_and_cfg_ranges():
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "seed": -1})
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "seed": 2**63})
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "cfg_scale": 21})
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "cfg_scale": "bad"})
    cleaned, _ = validate_generate(None, {**GOOD, "seed": 831001, "cfg_scale": 1.5})
    assert cleaned["seed"] == 831001 and cleaned["cfg_scale"] == 1.5


def test_sampling_overrides():
    cleaned, _ = validate_generate(None, {**GOOD, "sampling": {"abc": {"temperature": 0.9, "min_tokens": 32, "max_tokens": 128}}})
    assert cleaned["sampling"]["abc"] == {"temperature": 0.9, "min_tokens": 32, "max_tokens": 128}
    assert cleaned["sampling"]["semantic"] is None
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "sampling": {"abc": {"unknown_key": 1}}})
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "sampling": {"abc": {"temperature": 99}}})
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "sampling": {"semantic": {"min_tokens": 500, "max_tokens": 100}}})
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "sampling": {"abc": {"top_k": 1.5}}})
    # min_tokens defaults to the checkpoint's 200, so max_tokens below that is rejected
    with pytest.raises(ValidationError, match="min_tokens"):
        validate_generate(None, {**GOOD, "sampling": {"abc": {"max_tokens": 128}}})


def test_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        validate_generate(None, {**GOOD, "bpm": 120})
    with pytest.raises(ValidationError):
        validate_transcribe({"audio": "x", "negative_prompt": "y"})


def test_transcribe_validation():
    cleaned, _ = validate_transcribe({"audio": "clip.wav"})
    assert cleaned["task"] == "full" and cleaned["preset"] == "default" and cleaned["max_seconds"] is None
    with pytest.raises(ValidationError):
        validate_transcribe({"audio": "clip.wav", "task": "sing"})
    with pytest.raises(ValidationError):
        validate_transcribe({"audio": "clip.wav", "max_seconds": 0})
    cleaned, _ = validate_transcribe({"audio": "clip.wav", "task": "melody-vocal", "max_seconds": 30.5})
    assert cleaned["task"] == "melody-vocal" and cleaned["max_seconds"] == 30.5


def test_decode_validation():
    cleaned, _ = validate_decode({"source": "my_song", "vae": "legacy"})
    assert cleaned == {"source": "my_song", "vae": "legacy"}
    with pytest.raises(ValidationError):
        validate_decode({"source": "../etc/passwd"})
    with pytest.raises(ValidationError):
        validate_decode({"source": "my_song", "vae": "other"})
    with pytest.raises(ValidationError):
        validate_decode({"vae": "standard"})


def test_plan_rejects_off():
    with pytest.raises(ValidationError, match="cot=off"):
        validate_plan(None, {**GOOD, "cot": "off"})
