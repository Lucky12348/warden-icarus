"""A2S queries (Steam query) and detection of player arrivals/departures by difference.

Icarus may return only the number of players, without names: in that case the panel shows
"2/8 joueurs" without a list and announces "un joueur a rejoint" without a name.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from .ui import md

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    server_name: str
    count: int
    max_players: int
    names: tuple[str, ...] | None  # None: names unavailable

    @property
    def summary(self) -> str:
        label = "joueur" if self.max_players <= 1 else "joueurs"
        return f"{self.count}/{self.max_players} {label}"


def build_snapshot(server_name: str, count: int, max_players: int, raw_names: list[str] | None) -> Snapshot:
    """Names are kept only if all are filled in and consistent with the count."""
    names: tuple[str, ...] | None = None
    if raw_names is not None:
        cleaned = [n.strip() for n in raw_names]
        if len(cleaned) == count and all(cleaned):
            names = tuple(sorted(cleaned, key=str.lower))
    return Snapshot(server_name, count, max_players, names)


async def query(host: str, port: int, timeout: float = 3.0) -> Snapshot | None:
    """Query the server; None if it does not answer (stopped, starting, updating...)."""
    import a2s  # local import: tests of the pure functions do not need it

    address = (host, port)
    try:
        info = await a2s.ainfo(address, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - timeout, invalid answer, closed port...
        log.debug("A2S info sans réponse : %s", e)
        return None
    raw_names: list[str] | None
    try:
        players = await a2s.aplayers(address, timeout=timeout)
        raw_names = [p.name or "" for p in players]
    except Exception as e:  # noqa: BLE001
        log.debug("A2S players sans réponse : %s", e)
        raw_names = None
    return build_snapshot(info.server_name, info.player_count, info.max_players, raw_names)


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def diff(previous: Snapshot | None, current: Snapshot | None) -> list[tuple[str, str]]:
    """Events ("join"/"leave", text) between two successful snapshots.

    A missing snapshot (A2S not answering) produces nothing: this avoids wrongly announcing
    that everyone left when the server simply stops answering.
    """
    if previous is None or current is None:
        return []
    suffix = f" ({current.summary})"
    events: list[tuple[str, str]] = []
    if previous.names is not None and current.names is not None:
        before, after = Counter(previous.names), Counter(current.names)
        for name in sorted((after - before).elements(), key=str.lower):
            events.append(("join", f"👋 **{md(name)}** a rejoint la partie{suffix}"))
        for name in sorted((before - after).elements(), key=str.lower):
            events.append(("leave", f"🚪 **{md(name)}** a quitté la partie{suffix}"))
        return events

    delta = current.count - previous.count
    if delta > 0:
        text = "Un joueur a rejoint" if delta == 1 else f"{delta} joueurs ont rejoint"
        events.append(("join", f"👋 {text} la partie{suffix}"))
    elif delta < 0:
        text = "Un joueur a quitté" if delta == -1 else f"{-delta} joueurs ont quitté"
        events.append(("leave", f"🚪 {text} la partie{suffix}"))
    return events


def players_line(snapshot: Snapshot | None) -> str:
    if snapshot is None:
        return "👥 Joueurs : — *(le serveur ne répond pas aux requêtes)*"
    line = f"👥 **{snapshot.summary}**"
    if snapshot.names:
        line += " : " + ", ".join(md(n) for n in snapshot.names)
    return line
