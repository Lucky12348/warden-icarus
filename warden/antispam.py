"""Anti-spam for log lines and alerts.

Two combined protections:
- deduplication: an "equivalent" line (digits and hexadecimal ids ignored) already seen
  within the last `dedupe_seconds` seconds is dropped;
- rate: at most `max_events` messages per sliding window of `window_seconds`.

Dropped messages are counted; `SummaryGate` publishes at most one summary per hour (LOG_SPAM_SUMMARY).
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
    """Deduplication key: timestamps, counters and addresses do not make a different line."""
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
        """True if the message may be published. `key` replaces the deduplication key."""
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
        """Number of messages dropped since the last call (reset to zero)."""
        count, self._suppressed = self._suppressed, 0
        return count

    def _expire(self, now: float) -> None:
        while self._sent and now - self._sent[0] >= self.window_seconds:
            self._sent.popleft()
        if self.dedupe_seconds > 0 and len(self._seen) > 1000:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self.dedupe_seconds}


class SummaryGate:
    """Accumulates dropped lines and allows at most one summary per `interval_seconds`."""

    def __init__(self, interval_seconds: float = 3600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._count = 0
        self._last = clock()  # first summary one interval after start-up at the earliest

    def add(self, count: int) -> None:
        self._count += count

    def pop_due(self) -> int:
        """Accumulated total if a summary is due (reset to zero), otherwise 0."""
        now = self._clock()
        if self._count == 0 or now - self._last < self.interval_seconds:
            return 0
        count, self._count, self._last = self._count, 0, now
        return count
