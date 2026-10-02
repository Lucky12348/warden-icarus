"""Clé « ignore » des motifs de logs : bruit d'Unreal/Wine jamais diffusé ni compté comme erreur."""

import json
from pathlib import Path

import pytest

from warden.logpatterns import DEFAULT_PATTERNS, LABELS, LogPatterns

EXAMPLE = Path(__file__).resolve().parent.parent / "patterns.example.json"

NOISE = [
    "wine: setlocale() failed, falling back to C locale",
    "error: XDG_RUNTIME_DIR not set in the environment.",
    "[2026.10.02-06.00.01:123][  0]LogFMOD: Error: FMOD error 18 - File not found",
    "[2026.10.02-06.00.02:456][  0]LogStreaming: Error: Couldn't find file for package /Game/Foo",
    "LogProperty: Error: Struct type unknown for property 'Bar'; perhaps the USTRUCT() was renamed",
    "LogStringTable: Warning: Failed to find string table entry for 'X'",
    "LogModuleManager: Error: Unable to load module 'CheatFunctions'",
    "LogModuleManager: Warning: FunctionalTesting failed to load",
    "LogInit: Error: AutomationScreenshot plugin missing",
]


@pytest.mark.parametrize("line", NOISE)
def test_default_ignore_drops_unreal_noise(line):
    assert LogPatterns.load(None).classify(line) is None


@pytest.mark.parametrize("line", NOISE)
def test_example_file_ignores_same_noise(line):
    assert LogPatterns.load(EXAMPLE).classify(line) is None


def test_example_file_ignore_matches_defaults():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert data["ignore"] == DEFAULT_PATTERNS["ignore"]


def test_real_errors_still_reported():
    match = LogPatterns.load(None).classify("LogNet: Error: connection refused")
    assert match is not None and match.category == "error"


def test_defaults_apply_when_file_has_no_ignore(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"join": ["Join succeeded: (?P<name>.+)"]}), encoding="utf-8")
    patterns = LogPatterns.load(path)
    assert patterns.classify("LogStreaming: Error: Couldn't find file") is None


def test_file_ignore_replaces_defaults(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"ignore": [r"^bruit"]}), encoding="utf-8")
    patterns = LogPatterns.load(path)
    assert patterns.classify("bruit: Error: rien de grave") is None
    assert patterns.classify("LogStreaming: Error: Couldn't find file").category == "error"


def test_already_up_to_date_is_startup_not_error():
    match = LogPatterns.load(EXAMPLE).classify("Success! App '2089300' already up to date.")
    assert match.category == "startup"
    assert LABELS["startup"] == "✅"
