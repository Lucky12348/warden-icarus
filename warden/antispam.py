"""Anti-spam pour la diffusion des logs et des alertes.

Deux protections cumulées :
- déduplication : une ligne « équivalente » (chiffres et identifiants hexadécimaux ignorés)
  déjà vue dans les `dedupe_seconds` dernières secondes est ignorée ;
- débit : au plus `max_events` messages par fenêtre glissante de `window_seconds`.

Les messages écartés sont comptés pour pouvoir signaler « N lignes ignorées ».
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Callable

_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{6,}\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")


def normalize(line: str) -> str:
    """Clé de déduplication : horodatages, compteurs et adresses n'en font pas une autre ligne."""
    key = _HEX.sub("#", line.strip().lower())
    key = _DIGITS.sub("#", key)
    return _SPACES.sub(" ", key)


class AntiSpam:
    def __init__(
        self,
        max_events: int,
        window_seconds: float = 60.0,
        dedupe_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_events < 1:
            raise ValueError("max_events doit être >= 1")
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.dedupe_seconds = dedupe_seconds
        self._clock = clock
        self._sent: deque[float] = deque()
        self._seen: dict[str, float] = {}
        self._suppressed = 0

    def allow(self, message: str, key: str | None = None) -> bool:
        """True si le message peut être publié. `key` remplace la clé de déduplication."""
        now = self._clock()
        self._expire(now)

        dedupe_key = key if key is not None else normalize(message)
        if self.dedupe_seconds > 0:
            last = self._seen.get(dedupe_key)
            if last is not None and now - last < self.dedupe_seconds:
                self._suppressed += 1
                return False

        if len(self._sent) >= self.max_events:
            self._suppressed += 1
            return False

        self._sent.append(now)
        if self.dedupe_seconds > 0:
            self._seen[dedupe_key] = now
        return True

    def pop_suppressed(self) -> int:
        """Nombre de messages écartés depuis le dernier appel (remis à zéro)."""
        count, self._suppressed = self._suppressed, 0
        return count

    def _expire(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= self.window_seconds:
            self._sent.popleft()
        if self.dedupe_seconds > 0 and len(self._seen) > 1000:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self.dedupe_seconds}
