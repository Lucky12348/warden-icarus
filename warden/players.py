"""Requêtes A2S (Steam query) et détection des arrivées/départs de joueurs par différence.

Icarus peut ne renvoyer que le nombre de joueurs, sans les noms : dans ce cas on affiche
« 2/8 joueurs » sans liste et on annonce « un joueur a rejoint » sans nom.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    server_name: str
    count: int
    max_players: int
    names: tuple[str, ...] | None  # None : noms indisponibles

    @property
    def summary(self) -> str:
        label = "joueur" if self.max_players <= 1 else "joueurs"
        return f"{self.count}/{self.max_players} {label}"


def build_snapshot(server_name: str, count: int, max_players: int, raw_names: list[str] | None) -> Snapshot:
    """Les noms ne sont retenus que s'ils sont tous renseignés et cohérents avec le nombre."""
    names: tuple[str, ...] | None = None
    if raw_names is not None:
        cleaned = [n.strip() for n in raw_names]
        if len(cleaned) == count and all(cleaned):
            names = tuple(sorted(cleaned, key=str.lower))
    return Snapshot(server_name, count, max_players, names)


async def query(host: str, port: int, timeout: float = 3.0) -> Snapshot | None:
    """Interroge le serveur ; None s'il ne répond pas (arrêté, en démarrage, mise à jour…)."""
    import a2s  # import local : les tests des fonctions pures n'en ont pas besoin

    address = (host, port)
    try:
        info = await a2s.ainfo(address, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - timeout, réponse invalide, port fermé…
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
    """Événements (« join »/« leave », texte) entre deux relevés réussis.

    Un relevé manquant (A2S sans réponse) ne produit rien : on évite d'annoncer à tort que
    tout le monde est parti quand le serveur ne répond simplement plus.
    """
    if previous is None or current is None:
        return []
    suffix = f" ({current.summary})"
    events: list[tuple[str, str]] = []
    if previous.names is not None and current.names is not None:
        before, after = Counter(previous.names), Counter(current.names)
        for name in sorted((after - before).elements(), key=str.lower):
            events.append(("join", f"👋 **{name}** a rejoint la partie{suffix}"))
        for name in sorted((before - after).elements(), key=str.lower):
            events.append(("leave", f"🚪 **{name}** a quitté la partie{suffix}"))
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
        line += " : " + ", ".join(snapshot.names)
    return line
