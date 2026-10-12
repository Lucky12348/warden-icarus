"""Sorting of the Icarus container's log lines: only a few categories are published.

Each line is first **normalised** (`normalize_line`): the mornedhels/icarus-server image
sends the output of its supervisord programs through rsyslog, which produces
`2026-10-12T18:38:27.416174+00:00 icarus supervisord: icarus-server <message>#015`
("#015" = carriage return escaped by rsyslog). The header and the suffix are removed; the
program name (icarus-server, icarus-updater...) is kept separately. Patterns apply to the
message alone, with `re.search`.

Patterns can be replaced, category by category, by a JSON file (LOG_PATTERNS_FILE, see
patterns.example.json). "join"/"leave" patterns may capture the player name in a group
named `name`.

Publication (whitelist, decided by the bot): join, leave, startup, update (only if a real
update happened) and crash. "error" is published only if ERROR_LOGS_TO_DISCORD=true;
"ignore" never is.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Evaluation order: the first matching category wins.
CATEGORIES = ("ignore", "crash", "join", "leave", "update", "startup", "error")

# Categories published in the logs channel ("error": only with ERROR_LOGS_TO_DISCORD).
PUBLISHED = frozenset({"crash", "join", "leave", "update", "startup"})

DEFAULT_PATTERNS: dict[str, list[str]] = {
    # Evaluated first: an ignored line is neither published nor counted by the anti-spam.
    # If the patterns file defines "ignore", its list fully replaces this one.
    "ignore": [
        # Empty lines and Wine noise ("0024:fixme:...", "0050:err:vulkan:...").
        r"^\s*$",
        r"^[0-9a-f]{4}:(?:fixme|err|warn|trace):",
        r"^(?:fixme|err|warn|trace):",
        # Repetitions compressed by rsyslog.
        r"^message repeated \d+ times:",
        # Normal Unreal Engine noise under Wine (false "❌").
        r"setlocale\(",
        r"XDG_RUNTIME_DIR",
        # Unreal warnings, except crash signs ("LogMemory: Warning: Freeing ... backup pool").
        r"\bLog\w+: Warning: (?!.*(?i:backup pool|out of memory|assertion failed|fatal error))",
        r"LogFMOD:",
        r"LogStreaming: Error",
        r"LogProperty: Error: Struct type unknown",
        r"LogIcarusGameStateRecording: Error",
        r"IcarusOSSLog: Error",
        r"CheatFunctions|FunctionalTesting|AutomationScreenshot",
        # Known steamcmd noise during updates.
        r"ILocalize::AddFile\(\) failed",
        r"Failed to init SDL priority manager",
        r"Failed to set thread priority",
        r"dlmopen steamservice\.so failed",
        # supervisorctl started while the program is already running (cron + button, e.g.).
        r"ERROR \((?:already started|not running)\)",
        r"already running - exiting",
    ],
    "crash": [
        r"(?i)\bfatal error\b",
        r"(?i)\bran out of memory\b",
        r"(?i)\bfreeing .* backup pool\b",
        r"(?i)\bassertion failed\b",
        r"(?i)\bunhandled exception\b",
        r"=== Critical error",
        # supervisord: the game stopped too quickly or can no longer start.
        r"exited: icarus-server \(.*not expected\)",
        r"gave up: icarus-server entered FATAL state",
    ],
    # Unknown for now (no connection logs available): detection through A2S.
    # Unreal Engine example: "LogNet: Join succeeded: (?P<name>.+)".
    "join": [],
    "leave": [],
    # End of a steamcmd run: published only if the installed build changed.
    "update": [
        r"Success! App '2089300' fully installed",
        r"Success! App '2089300' already up to date",
    ],
    "startup": [r"Engine is initialized"],
    # No longer published by default (ERROR_LOGS_TO_DISCORD=true to enable it again).
    "error": [
        r"(?i)\berror\b",
        r"(?i)\bfailed\b",
        r"\bFATAL - ",
    ],
}

# rsyslog header: date (RFC 3339 or "Oct 12 06:00:03"), host, tag[pid]:
_SYSLOG = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?|[A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2})"
    r"\s+\S+\s+(?P<tag>[^\s:\[]+)(?:\[\d+\])?:\s?"
)
# supervisord prefixes each program's output with its name.
_PROGRAM = re.compile(r"^(?P<prog>icarus-[\w-]+|crond|rsyslogd)(?: |$)")  # $: empty message
# Carriage return: raw, or escaped by rsyslog (#015, possibly repeated).
_CR = re.compile(r"(?:#015|\r)+")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

LABELS = {
    "crash": "💥",
    "join": "👋",
    "leave": "🚪",
    "update": "⬆️",
    "startup": "✅",
    "error": "❌",
}


@dataclass(frozen=True)
class LogLine:
    """Normalised line: message alone, and originating supervisord program if known."""

    text: str
    program: str | None = None

    @property
    def display(self) -> str:
        return f"[{self.program}] {self.text}" if self.program else self.text


@dataclass(frozen=True)
class Match:
    category: str
    line: str  # display form ("[program] message")
    name: str | None = None
    program: str | None = None


def normalize_line(raw: str) -> LogLine:
    """Remove ANSI colours, the rsyslog/supervisord header and carriage returns (#015, \\r);
    tabs escaped by rsyslog (#011) become tabs again."""
    line = _CR.sub("", _ANSI.sub("", raw)).replace("#011", "\t").strip()  # #011: escaped tab
    program = None
    m = _SYSLOG.search(line)
    if m is not None:
        line = line[m.end():]
        if m.group("tag") == "supervisord":
            p = _PROGRAM.search(line)
            if p is not None:
                program = p.group("prog")
                line = line[p.end():]
        else:
            program = m.group("tag")
    return LogLine(line.strip(), program)


class LogPatterns:
    def __init__(self, patterns: dict[str, list[str]]) -> None:
        self.compiled = {cat: [re.compile(p) for p in patterns.get(cat, [])] for cat in CATEGORIES}

    @classmethod
    def load(cls, path: Path | None) -> "LogPatterns":
        """Default patterns, optionally replaced per category from a JSON file."""
        patterns = {k: list(v) for k, v in DEFAULT_PATTERNS.items()}
        if path is not None:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Le fichier de motifs doit contenir un objet JSON")
            for cat, values in data.items():
                if cat not in CATEGORIES:
                    raise ValueError(f"Catégorie inconnue dans {path} : {cat}")
                if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                    raise ValueError(f"{cat} doit être une liste de chaînes")
                patterns[cat] = values
        return cls(patterns)

    def classify(self, raw_line: str) -> Match | None:
        """Category of the line (after normalisation), or None (ignored / of no interest)."""
        line = normalize_line(raw_line)
        for cat in CATEGORIES:
            for rx in self.compiled[cat]:
                m = rx.search(line.text)
                if m is None:
                    continue
                if cat == "ignore":
                    return None
                name = m.groupdict().get("name") if cat in ("join", "leave") else None
                return Match(cat, line.display, name.strip() if name else None, line.program)
        return None


def should_publish(category: str, error_logs: bool = False) -> bool:
    """Whitelist of the logs channel ("update" is additionally filtered by the bot: real update)."""
    return category in PUBLISHED or (category == "error" and error_logs)
