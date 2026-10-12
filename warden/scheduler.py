"""Daily scheduled restart (equivalent of the old bot's restartcrontab "0 6 * * *").

Rules:
- announced in the logs channel 5 min before the planned time;
- at the planned time, no restart if players are connected (A2S): new attempt every
  15 min, for 2 h at most, then give up for the day;
- server already stopped: nothing to do.

`decide()` is pure (no I/O) so it is easy to test; the bot loop calls it every 30 s.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, time, timedelta

ANNOUNCE_BEFORE = timedelta(minutes=5)
RETRY_EVERY = timedelta(minutes=15)
MAX_DELAY = timedelta(hours=2)


def parse_hhmm(value: str) -> time:
    value = value.strip()
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Heure invalide : {value!r} (format HH:MM attendu)")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Heure invalide : {value!r}")
    return time(hour, minute)


class Action(enum.Enum):
    NONE = "none"
    ANNOUNCE = "announce"
    RESTART = "restart"
    POSTPONE = "postpone"
    GIVE_UP = "give_up"
    SKIP_STOPPED = "skip_stopped"


@dataclass
class Run:
    """Tracking of one occurrence (a given day) of the scheduled restart."""

    target: datetime
    announced: bool = False
    done: bool = False
    attempts: int = 0
    next_attempt: datetime | None = None

    @property
    def deadline(self) -> datetime:
        return self.target + MAX_DELAY


@dataclass(frozen=True)
class Decision:
    action: Action
    run: Run | None
    players: int | None = None


def next_target(now: datetime, at: time) -> datetime:
    """Next occurrence that is still "active" (2 h retry window included)."""
    tz = now.tzinfo
    for offset in (0, 1):
        day = now.date() + timedelta(days=offset)
        target = datetime.combine(day, at, tzinfo=tz)
        if now <= target + MAX_DELAY:
            return target
    raise AssertionError("inaccessible")  # pragma: no cover


def decide(
    now: datetime,
    enabled: bool,
    at: time,
    run: Run | None,
    running: bool,
    players: int | None,
    restart_if_a2s_down: bool = False,
) -> Decision:
    """`players` is None when A2S does not answer (unknown number of players)."""
    if not enabled:
        return Decision(Action.NONE, None)

    target = next_target(now, at)
    if run is None or run.target != target:
        run = Run(target)
    if run.done or now < target - ANNOUNCE_BEFORE:
        return Decision(Action.NONE, run)

    if now < target:
        if run.announced:
            return Decision(Action.NONE, run)
        run.announced = True
        return Decision(Action.ANNOUNCE, run)

    if run.next_attempt is not None and now < run.next_attempt:
        return Decision(Action.NONE, run)

    if not running:
        run.done = True
        return Decision(Action.SKIP_STOPPED, run)

    run.attempts += 1
    unknown = players is None and not restart_if_a2s_down
    busy = unknown or (players is not None and players > 0)
    if not busy:
        run.done = True
        return Decision(Action.RESTART, run, players)

    if now >= run.deadline:
        run.done = True
        return Decision(Action.GIVE_UP, run, players)
    run.next_attempt = min(now + RETRY_EVERY, run.deadline)
    return Decision(Action.POSTPONE, run, players)
