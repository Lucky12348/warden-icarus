"""Classification of the Icarus container's Docker events.

Cases to tell apart:
- actions started by the bot (Discord or scheduler): expected, no alert;
- clean stop (code 0) with no outside request: a normal stop of the container (with the old
  image, the empty-server shutdown), restarted by `restart: unless-stopped`: no alert, no
  message;
- exit with an error (code != 0) or OOM with no request: **crash**;
- restart by Docker after a crash: **restart after crash**;
- `docker restart` outside Discord: **unexpected restart**;
- `docker stop`/`kill`/`start` outside Discord: plain information.

With mornedhels/icarus-server, a game crash is restarted by supervisord inside the
container and produces no Docker event: it shows up in the logs instead (logpatterns).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

KILL_WINDOW = 120.0  # an exit less than 2 min after an outside kill/stop = requested stop
RECENT_DIE = 900.0  # a restart less than 15 min after an exit is linked to it
OOM_WINDOW = 30.0
BOT_GRACE = 30.0  # late events after the end of a bot action

CRASH = "crash"
RESTART_AFTER_CRASH = "restart_after_crash"
UNEXPECTED_RESTART = "unexpected_restart"
MANUAL_STOP = "manual_stop"
EXTERNAL_START = "external_start"
EXPECTED = "expected"


@dataclass(frozen=True)
class Classified:
    kind: str
    action: str
    exit_code: int | None = None
    oom: bool = False


@dataclass
class _Die:
    at: float
    code: int
    kind: str  # "crash", "clean", "manual", "bot"


class EventClassifier:
    def __init__(
        self,
        clean_exit_codes: frozenset[int] = frozenset({0}),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clean_exit_codes = clean_exit_codes
        self._clock = clock
        self._operation: str | None = None
        self._operation_ended_at: float | None = None
        self._last_kill: float | None = None
        self._last_oom: float | None = None
        self._last_die: _Die | None = None
        self._crash_restart_reported = False
        self._manual_reported_at: float | None = None

    # --- bot actions ---
    def begin_operation(self, name: str) -> None:
        self._operation = name
        self._operation_ended_at = None

    def end_operation(self) -> None:
        self._operation = None
        self._operation_ended_at = self._clock()

    def bot_operation_active(self) -> bool:
        if self._operation is not None:
            return True
        ended = self._operation_ended_at
        return ended is not None and self._clock() - ended < BOT_GRACE

    def _recent(self, at: float | None, window: float) -> bool:
        return at is not None and self._clock() - at < window

    # --- classification ---
    def classify(self, event: dict[str, Any]) -> Classified | None:
        if event.get("Type", "container") != "container":
            return None
        action = str(event.get("Action") or event.get("status") or "")
        attrs = (event.get("Actor") or {}).get("Attributes") or {}
        now = self._clock()

        if action.startswith(("exec_", "health_status", "attach", "resize", "top", "copy", "archive")):
            return None

        if self.bot_operation_active():
            if action == "die":
                self._last_die = _Die(now, _exit_code(attrs), "bot")
            return Classified(EXPECTED, action)

        if action == "kill":
            self._last_kill = now
            return None

        if action == "oom":
            self._last_oom = now
            return None

        if action == "die":
            code = _exit_code(attrs)
            if self._recent(self._last_oom, OOM_WINDOW):
                return self._crash(now, code, oom=True)
            if self._recent(self._last_kill, KILL_WINDOW):
                self._last_die = _Die(now, code, "manual")
                self._manual_reported_at = now
                return Classified(MANUAL_STOP, action, code)
            if code in self._clean_exit_codes:
                self._last_die = _Die(now, code, "clean")
                return None
            return self._crash(now, code, oom=False)

        if action == "stop":
            if self._recent(self._manual_reported_at, KILL_WINDOW):
                return None
            self._manual_reported_at = now
            return Classified(MANUAL_STOP, action)

        if action in ("start", "restart"):
            die = self._last_die
            if die is not None and now - die.at < RECENT_DIE:
                if die.kind == "crash":
                    if self._crash_restart_reported:
                        return None
                    self._crash_restart_reported = True
                    return Classified(RESTART_AFTER_CRASH, action, die.code)
                if die.kind in ("clean", "bot"):
                    return None
                if die.kind == "manual" and now - die.at < KILL_WINDOW:
                    # docker restart: kill -> die -> stop -> start -> restart
                    return Classified(UNEXPECTED_RESTART, action) if action == "restart" else None
            return Classified(UNEXPECTED_RESTART if action == "restart" else EXTERNAL_START, action)

        return None

    def _crash(self, now: float, code: int, oom: bool) -> Classified:
        self._last_die = _Die(now, code, "crash")
        self._crash_restart_reported = False
        return Classified(CRASH, "die", code, oom)


def _exit_code(attrs: dict[str, Any]) -> int:
    try:
        return int(attrs.get("exitCode", 0))
    except (TypeError, ValueError):
        return -1
