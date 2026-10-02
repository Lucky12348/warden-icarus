"""Tri des lignes de logs du conteneur Icarus : seules les lignes « importantes » sont diffusées.

Les motifs par défaut sont volontairement prudents. Ils sont remplaçables, catégorie par
catégorie, par un fichier JSON (LOG_PATTERNS_FILE, voir patterns.example.json). Les motifs
« join »/« leave » peuvent capturer le nom du joueur dans un groupe nommé `name`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Ordre d'évaluation : la première catégorie qui correspond l'emporte.
CATEGORIES = ("ignore", "crash", "join", "leave", "update", "startup", "error")

DEFAULT_PATTERNS: dict[str, list[str]] = {
    # Bruit de Wine (« 0024:fixme:… », « err:ole:… ») et lignes vides.
    "ignore": [r"^\s*$", r"^[0-9a-f]{4}:(?:fixme|err|warn|trace):", r"^(?:fixme|err|warn|trace):"],
    "crash": [
        r"=== Critical error",
        r"(?i)\bsegmentation fault\b",
        r"(?i)\bunhandled exception\b",
        r"(?i)\bfatal error\b",
        r"(?i)\bwine: .*(?:crash|exception)",
    ],
    # Inconnus pour l'instant (pas de logs de connexion disponibles) : détection par A2S.
    # Exemple Unreal Engine : "LogNet: Join succeeded: (?P<name>.+)".
    "join": [],
    "leave": [],
    "update": [
        r"Success! App '2089300' fully installed",
        r"(?i)update state \(0x\w+\) downloading",
    ],
    "startup": [
        r"Engine is initialized",
        r"(?i)\bserver (?:is )?(?:ready|started|listening)\b",
        r"Success! App '2089300' already up to date",
    ],
    "error": [r"(?i)\berror\b", r"(?i)\bfailed\b"],
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

LABELS = {
    "crash": "💥",
    "join": "👋",
    "leave": "🚪",
    "update": "⬆️",
    "startup": "🚀",
    "error": "❌",
}


@dataclass(frozen=True)
class Match:
    category: str
    line: str
    name: str | None = None


class LogPatterns:
    def __init__(self, patterns: dict[str, list[str]]) -> None:
        self.compiled = {cat: [re.compile(p) for p in patterns.get(cat, [])] for cat in CATEGORIES}

    @classmethod
    def load(cls, path: Path | None) -> "LogPatterns":
        """Motifs par défaut, éventuellement remplacés par catégorie depuis un fichier JSON."""
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
        line = _ANSI.sub("", raw_line).rstrip()
        for cat in CATEGORIES:
            for rx in self.compiled[cat]:
                m = rx.search(line)
                if m is None:
                    continue
                if cat == "ignore":
                    return None
                name = m.groupdict().get("name") if cat in ("join", "leave") else None
                return Match(cat, line.strip(), name.strip() if name else None)
        return None
