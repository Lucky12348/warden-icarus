"""Log normalisation and sorting: only a few categories are published, Unreal/Wine noise never."""

import json
from pathlib import Path

import pytest

from warden.logpatterns import DEFAULT_PATTERNS, LABELS, LogPatterns, normalize_line, should_publish

EXAMPLE = Path(__file__).resolve().parent.parent / "patterns.example.json"
STAMP = "2026-10-12T18:38:27.416174+00:00"


def real(message, program="icarus-server", stamp=STAMP, cr="#015"):
    """Line as the mornedhels image produces it (rsyslog + supervisord, escaped carriage return)."""
    return f"{stamp} icarus supervisord: {program} {message}{cr}"


def published(patterns, line, error_logs=False):
    match = patterns.classify(line)
    return match is not None and should_publish(match.category, error_logs)


# --------------------------------------------------------------------------- normalisation
@pytest.mark.parametrize("cr", ["#015", "\r", "#015#015", ""])
def test_normalize_strips_prefix_and_carriage_return(cr):
    line = normalize_line(real("[2026.10.12-18.38.27:416][  0]LogInit: Display: Engine is initialized.", cr=cr))
    assert line.text == "[2026.10.12-18.38.27:416][  0]LogInit: Display: Engine is initialized."
    assert line.program == "icarus-server"
    assert line.display == "[icarus-server] [2026.10.12-18.38.27:416][  0]LogInit: Display: Engine is initialized."


@pytest.mark.parametrize("stamp", [STAMP, "2026-10-12T18:38:27Z", "2026-10-12T18:38:27+0200", "Oct 12 18:38:27", "Oct  2 18:38:27"])
def test_normalize_accepts_timestamp_variants(stamp):
    line = normalize_line(real("Success! App '2089300' fully installed.", "icarus-updater", stamp))
    assert (line.text, line.program) == ("Success! App '2089300' fully installed.", "icarus-updater")


def test_normalize_keeps_unprefixed_lines():
    assert normalize_line("plain line\r") == normalize_line("plain line")
    assert normalize_line("2026-10-12 06:00:03,123 INFO exited: icarus-updater (exit status 0; expected)").program is None


def test_normalize_rsyslog_repeat_line_has_no_program():
    line = normalize_line(f"{STAMP} icarus supervisord: message repeated 6 times: [ icarus-server LogTemp: Warning: x]#015")
    assert line.program is None and line.text.startswith("message repeated 6 times:")


# --------------------------------------------------------------------------- real lines
NOT_PUBLISHED = [
    "[2026.10.12-18.38.27:416][123]LogTalentGame: Warning: UTalentComponent::UnlockTalent UnlockTalent Failed - Seed_Pouch 1",
    "[2026.10.12-18.38.27:416][123]LogTemp: Warning: GetDerivedClasses failed to find singleton",
    "[2026.10.12-18.38.27:416][123]IcarusOSSLog: Error: Login() failed: Empty Id",
    "[2026.10.12-18.38.27:416][123]IcarusOSSLog: Error: OnResUserTicket : No player found",
    "0050:err:vulkan:vulkan_init_once Failed to load libvulkan.so.1",
    "[2026.10.12-18.38.27:416][123]LogIcarusGameStateRecording: Error: ResolveOwnerRecorderState - Could not resolve owner",
    "[2026.10.12-18.38.27:416][123]LogAnimMontage: Warning: JumpToSectionName Loop bEndOfSection: 0 failed",
    # Already known noise.
    "wine: setlocale() failed, falling back to C locale",
    "error: XDG_RUNTIME_DIR not set in the environment.",
    "[2026.10.02-06.00.01:123][  0]LogFMOD: Error: FMOD error 18 - File not found",
    "[2026.10.02-06.00.02:456][  0]LogStreaming: Error: Couldn't find file for package /Game/Foo",
    "LogProperty: Error: Struct type unknown for property 'Bar'; perhaps the USTRUCT() was renamed",
    "LogStringTable: Warning: Failed to find string table entry for 'X'",
    "LogModuleManager: Error: Unable to load module 'CheatFunctions'",
    # Generic errors: no longer published by default.
    "[2026.10.12-18.38.27:416][123]LogNet: Error: connection refused",
    "INFO - Running icarus-server",
    "WARN - Download of ICARUS server failed - retry",
]


@pytest.mark.parametrize("message", NOT_PUBLISHED)
def test_real_noise_is_not_published(message):
    patterns = LogPatterns.load(None)
    assert not published(patterns, real(message))
    assert not published(LogPatterns.load(EXAMPLE), real(message))


def test_rsyslog_repeat_line_is_ignored():
    line = f"{STAMP} icarus supervisord: message repeated 6 times: [ icarus-server LogNet: Error: x Fatal error]#015"
    assert LogPatterns.load(None).classify(line) is None


@pytest.mark.parametrize(
    "line,category",
    [
        (real("[2026.10.12-18.40.00:001][  0]LogInit: Display: Engine is initialized."), "startup"),
        (real("Success! App '2089300' fully installed.", "icarus-updater"), "update"),
        (real("Success! App '2089300' already up to date.", "icarus-updater"), "update"),
        (real("[2026.10.12-18.40.00:001][ 12]LogWindows: Error: Fatal error: [File:Unknown] [Line: 197]"), "crash"),
        (real("Ran out of memory allocating 0 bytes with alignment 0"), "crash"),
        (real("[2026.10.12-18.40.00:001][ 12]LogMemory: Warning: Freeing 33554432 bytes from backup pool to handle out of memory."), "crash"),
        (real("[2026.10.12-18.40.00:001][ 12]LogOutputDevice: Error: Assertion failed: IsValid(Actor)"), "crash"),
        (real("Unhandled Exception: EXCEPTION_ACCESS_VIOLATION reading address 0x0"), "crash"),
        ("2026-10-12 06:00:03,123 WARN exited: icarus-server (exit status 1; not expected)", "crash"),
        ("2026-10-12 06:00:03,123 INFO gave up: icarus-server entered FATAL state, too many start retries too quickly", "crash"),
    ],
)
def test_whitelisted_lines_are_published(line, category):
    match = LogPatterns.load(None).classify(line)
    assert match is not None and match.category == category
    assert should_publish(match.category)


def test_published_line_keeps_program_as_metadata():
    match = LogPatterns.load(None).classify(real("Success! App '2089300' fully installed.", "icarus-updater"))
    assert match.program == "icarus-updater"
    assert match.line == "[icarus-updater] Success! App '2089300' fully installed."
    assert "#015" not in match.line and "\r" not in match.line
    assert LABELS["update"] == "⬆️"


def test_error_category_only_with_option():
    patterns = LogPatterns.load(None)
    line = real("[2026.10.12-18.38.27:416][123]LogNet: Error: connection refused")
    assert patterns.classify(line).category == "error"
    assert not published(patterns, line)
    assert published(patterns, line, error_logs=True)
    # Noise stays ignored even with the option.
    assert not published(patterns, real("IcarusOSSLog: Error: Login() failed: Empty Id"), error_logs=True)
    assert not published(patterns, real("LogTemp: Warning: GetDerivedClasses failed to find singleton"), error_logs=True)


def test_should_publish_whitelist():
    assert {c for c in ("ignore", "crash", "join", "leave", "update", "startup", "error") if should_publish(c)} == {
        "crash", "join", "leave", "update", "startup"
    }


@pytest.mark.parametrize(
    "line",
    [
        real("0024:fixme:ntdll:NtQuerySystemInformation info"),
        real("ILocalize::AddFile() failed to load file \"public/steambootstrapper_english.txt\".", "icarus-updater"),
        real("icarus-updater: ERROR (already started)", "icarus-bootstrap"),
        real("FATAL - Process with PID 123 already running - exiting", "icarus-updater"),
    ],
)
def test_image_noise_is_ignored(line):
    assert LogPatterns.load(None).classify(line) is None


# --------------------------------------------------------------------------- patterns.json
def test_example_file_ignore_matches_defaults():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert data["ignore"] == DEFAULT_PATTERNS["ignore"]


def test_custom_file_gets_same_normalization(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"join": [r"^LogNet: Join succeeded: (?P<name>.+)$"]}), encoding="utf-8")
    match = LogPatterns.load(path).classify(real("LogNet: Join succeeded: Alice"))
    assert match.category == "join" and match.name == "Alice"  # ^ and $ work: prefix and #015 removed


def test_defaults_apply_when_file_has_no_ignore(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"join": ["Join succeeded: (?P<name>.+)"]}), encoding="utf-8")
    assert LogPatterns.load(path).classify(real("LogStreaming: Error: Couldn't find file")) is None


def test_file_ignore_replaces_defaults(tmp_path):
    path = tmp_path / "patterns.json"
    path.write_text(json.dumps({"ignore": [r"^bruit"]}), encoding="utf-8")
    patterns = LogPatterns.load(path)
    assert patterns.classify(real("bruit: Error: rien de grave")) is None
    assert patterns.classify(real("LogStreaming: Error: Couldn't find file")).category == "error"


def test_normalize_restores_escaped_tabs():
    line = normalize_line(real("LogInit: Display:#011Engine is initialized."))
    assert line.text == "LogInit: Display:\tEngine is initialized."
