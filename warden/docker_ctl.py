"""Accès Docker restreint au seul conteneur Icarus.

Le socket Docker équivaut à un accès root sur la VM. Ce module est le **seul** endroit du
bot qui l'utilise, et il n'expose qu'une liste fermée d'opérations sur un conteneur nommé :
état, statistiques, logs, événements, start/stop/restart, et recréation via le compose
d'Icarus (dossier vérifié). Pas d'exec, pas de création de conteneur arbitraire, pas
d'accès aux autres conteneurs.

Toutes les méthodes sont bloquantes : le bot les appelle via `asyncio.to_thread`.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

_COMPOSE_ENV_PASSTHROUGH = ("PATH", "DOCKER_HOST", "DOCKER_CONFIG", "HOME")


class DockerError(Exception):
    pass


@dataclass(frozen=True)
class ContainerStatus:
    exists: bool
    state: str = "missing"  # created, running, restarting, exited, paused, dead, missing
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    oom_killed: bool = False
    restart_count: int = 0
    image: str = ""
    short_id: str = ""
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    ports: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True)
class Stats:
    cpu_percent: float | None
    mem_used: int | None
    mem_limit: int | None


def parse_docker_time(value: str | None) -> datetime | None:
    """« 2026-10-02T06:00:03.123456789Z » → datetime UTC (None pour la date zéro de Docker)."""
    if not value or value.startswith("0001-01-01"):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$", value)
    if not m:
        return None
    base, frac, tz = m.groups()
    micro = (frac or "0")[:6].ljust(6, "0")
    dt = datetime.fromisoformat(f"{base}.{micro}")
    if tz in (None, "Z"):
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    hours, minutes = int(tz[1:3]), int(tz[4:6])
    return dt.replace(tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)))


def compute_cpu_percent(stats: dict[str, Any]) -> float | None:
    try:
        cpu, pre = stats["cpu_stats"], stats["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
        cpus = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or 1
    except (KeyError, TypeError):
        return None
    if sys_delta <= 0 or cpu_delta < 0:
        return 0.0
    return cpu_delta / sys_delta * cpus * 100.0


def compute_memory(stats: dict[str, Any]) -> tuple[int | None, int | None]:
    """Mémoire utilisée hors cache (même calcul que `docker stats`), et limite."""
    mem = stats.get("memory_stats") or {}
    usage, limit = mem.get("usage"), mem.get("limit")
    if usage is None:
        return None, limit
    inner = mem.get("stats") or {}
    cache = inner.get("inactive_file", inner.get("total_inactive_file", 0)) or 0
    return max(0, usage - cache), limit


def _format_ports(attrs: dict[str, Any]) -> list[str]:
    out = []
    bindings = (attrs.get("HostConfig") or {}).get("PortBindings") or {}
    for container_port, hosts in sorted(bindings.items()):
        for host in hosts or []:
            out.append(f"{host.get('HostPort')}→{container_port}")
    return out


class IcarusDocker:
    def __init__(self, container_name: str, icarus_dir: Path, client: Any = None) -> None:
        self.name = container_name
        self.icarus_dir = icarus_dir
        if client is None:
            import docker  # import local : les tests n'ont pas besoin du SDK

            client = docker.from_env()
        self.client = client

    # --- lecture ---
    def _container(self) -> Any:
        import docker.errors

        try:
            container = self.client.containers.get(self.name)
        except docker.errors.NotFound:
            raise DockerError(f"Conteneur « {self.name} » introuvable") from None
        if container.name != self.name:  # ne jamais agir sur un autre conteneur (préfixe d'ID…)
            raise DockerError(f"Résolution inattendue : {container.name} au lieu de {self.name}")
        return container

    def status(self) -> ContainerStatus:
        try:
            container = self._container()
        except DockerError:
            return ContainerStatus(exists=False)
        attrs = container.attrs
        st = attrs.get("State") or {}
        cfg = attrs.get("Config") or {}
        from .settings_store import container_env

        return ContainerStatus(
            exists=True,
            state=st.get("Status", "unknown"),
            started_at=parse_docker_time(st.get("StartedAt")),
            finished_at=parse_docker_time(st.get("FinishedAt")),
            exit_code=st.get("ExitCode"),
            oom_killed=bool(st.get("OOMKilled")),
            restart_count=int(attrs.get("RestartCount") or 0),
            image=cfg.get("Image", ""),
            short_id=container.short_id,
            env=container_env(cfg.get("Env")),
            labels=dict(cfg.get("Labels") or {}),
            ports=_format_ports(attrs),
        )

    def stats(self) -> Stats:
        container = self._container()
        raw = container.stats(stream=False)
        used, limit = compute_memory(raw)
        return Stats(compute_cpu_percent(raw), used, limit)

    def logs(self, since: int) -> Iterator[str]:
        """Suit les logs (bloquant) à partir de `since` ; se termine à l'arrêt du conteneur."""
        container = self._container()
        buffer = b""
        for chunk in container.logs(stream=True, follow=True, since=since, stdout=True, stderr=True):
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                yield line.decode("utf-8", errors="replace").rstrip("\r")
        if buffer:
            yield buffer.decode("utf-8", errors="replace")

    def events(self) -> Iterator[dict[str, Any]]:
        """Événements Docker du seul conteneur Icarus (bloquant, infini)."""
        stream = self.client.events(decode=True, filters={"type": "container", "container": self.name})
        try:
            for event in stream:
                attrs = (event.get("Actor") or {}).get("Attributes") or {}
                if attrs.get("name") == self.name:
                    yield event
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()

    # --- actions ---
    def start(self) -> None:
        self._container().start()

    def stop(self, timeout: int) -> None:
        self._container().stop(timeout=timeout)

    def restart(self, timeout: int) -> None:
        self._container().restart(timeout=timeout)

    def compose_target(self) -> tuple[str, str, list[str], str]:
        """(projet, dossier, fichiers compose, service) d'après les labels du conteneur.

        Refuse toute recréation si le projet compose n'est pas dans ICARUS_DIR.
        """
        labels = self._container().labels
        project = labels.get("com.docker.compose.project")
        workdir = labels.get("com.docker.compose.project.working_dir")
        service = labels.get("com.docker.compose.service")
        files = labels.get("com.docker.compose.project.config_files", "")
        if not (project and workdir and service):
            raise DockerError("Le conteneur n'a pas été créé par docker compose : recréation impossible.")
        base = os.path.realpath(self.icarus_dir)
        if os.path.realpath(workdir) != base:
            raise DockerError(f"Projet compose inattendu ({workdir}), attendu {self.icarus_dir}.")
        config_files = [f for f in files.split(",") if f] or [str(self.icarus_dir / "docker-compose.yml")]
        for f in config_files:
            if os.path.dirname(os.path.realpath(f)) != base:
                raise DockerError(f"Fichier compose hors de {self.icarus_dir} : {f}")
        return project, workdir, config_files, service

    def _compose(self, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        project, workdir, files, _ = self.compose_target()
        cmd = ["docker", "compose", "--project-name", project, "--project-directory", workdir]
        for f in files:
            cmd += ["-f", f]
        cmd += args
        # Environnement minimal : une variable du bot ne doit pas écraser une valeur du .env d'Icarus.
        env = {k: os.environ[k] for k in _COMPOSE_ENV_PASSTHROUGH if k in os.environ}
        log.info("Exécution : %s", " ".join(cmd))
        return subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout, check=False)

    def compose_check(self) -> None:
        """Valide le compose + .env (syntaxe, interpolation) sans rien modifier."""
        result = self._compose(["config", "--quiet"], timeout=60)
        if result.returncode != 0:
            raise DockerError(f"docker compose config a échoué : {(result.stderr or result.stdout).strip()[-500:]}")

    def recreate(self, stop_timeout: int, timeout: int) -> None:
        """Recrée le conteneur avec la configuration actuelle du compose et du .env."""
        _, _, _, service = self.compose_target()
        status = self.status()
        if status.running:
            self.stop(stop_timeout)  # arrêt propre avec le délai du bot (sauvegarde du jeu)
        result = self._compose(
            ["up", "-d", "--no-deps", "--force-recreate", "--timeout", str(stop_timeout), service],
            timeout=timeout,
        )
        if result.returncode != 0:
            raise DockerError(f"docker compose up a échoué : {(result.stderr or result.stdout).strip()[-500:]}")


def read_buildid(game_dir: Path, app_id: str = "2089300") -> str | None:
    """Build Steam installé (appmanifest), pour détecter les mises à jour au démarrage."""
    manifest = game_dir / "steamapps" / f"appmanifest_{app_id}.acf"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'"buildid"\s+"(\d+)"', text)
    return m.group(1) if m else None
