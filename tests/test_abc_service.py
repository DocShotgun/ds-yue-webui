"""abc_service tests against the official abc_tools module (real YuE checkout)."""
import pytest

from server import abc_service
from server.validation import ValidationError

from conftest import load_sample_jazz, load_sample_score


def test_inspect_valid(settings):
    result = abc_service.inspect_abc(settings, load_sample_score(settings))
    assert result["ok"], result.get("error")
    report = result["report"]
    assert result["has_chords"] is True
    assert report["voices"]["Vocal"]["sounding_notes"] > 0
    # the official example scores carry rests in the Ins voice; measures must parse
    assert report["voices"]["Ins"]["measures"] > 0
    assert isinstance(report["duration_quarters"], str)


def test_inspect_invalid(settings):
    result = abc_service.inspect_abc(settings, "this is not a score")
    assert not result["ok"]
    assert result["error"]


def test_strip_chords_removes_chords(settings):
    sample_score = load_sample_score(settings)
    result = abc_service.strip_chords(settings, sample_score, "both")
    assert result["ok"]
    stripped = result["abc"]
    check = abc_service.inspect_abc(settings, stripped)
    assert check["ok"] and check["has_chords"] is False
    before = abc_service.inspect_abc(settings, sample_score)["report"]
    after = check["report"]
    assert after["voices"]["Vocal"]["sounding_notes"] == before["voices"]["Vocal"]["sounding_notes"]


def test_strip_chords_invalid_voice(settings):
    with pytest.raises(ValueError):
        abc_service.strip_chords(settings, load_sample_score(settings), "wrong")


def test_compare_unchanged_harmony_edit(settings):
    """The official example: score-jazz.abc changes only chord symbols."""
    result = abc_service.compare_abc(settings, load_sample_score(settings), load_sample_jazz(settings))
    assert result["ok"], result.get("error")
    assert result["compare"]["match"] is True


def test_compare_detects_note_changes(settings):
    sample_score = load_sample_score(settings)
    result = abc_service.compare_abc(settings, sample_score, sample_score, voices="both")
    assert result["ok"] and result["compare"]["match"] is True


def test_import_error_message():
    class FakeSettings:
        abc_tools_path = "/nonexistent/abc_tools.py"

    with pytest.raises(FileNotFoundError):
        abc_service.inspect_abc(FakeSettings(), "X:1")
