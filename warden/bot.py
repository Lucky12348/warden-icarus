"""Discord client: persistent panels, monitoring loops and server operations."""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

import discord
from discord import app_commands

from . import __version__
from . import backups as bk
from . import players as pl
from . import scheduler as sch
from . import safefs
from . import settings_store as ss
from . import ui
from .config import Config
from .docker_ctl import ContainerStatus, DockerError, IcarusDocker, Stats, read_buildid
from .events import (
    CRASH,
    EXTERNAL_START,
    MANUAL_STOP,
    RESTART_AFTER_CRASH,
    UNEXPECTED_RESTART,
    EventClassifier,
)
from .logpatterns import LABELS, LogPatterns, normalize_line, should_publish
from .notify import Notifier
from .panels import ServerInfo, SettingsInfo, backups_panel, server_panel, settings_panel
from .pending import Pending
from .state import ALERT_KEYS, State

log = logging.getLogger(__name__)

HEARTBEAT_FILE = Path("/tmp/warden-heartbeat")
INVITE_PERMISSIONS = 199680  # View channels, Send, Read history, Mention roles
# A "fully installed" line is published if the build changed in the last 10 minutes.
UPDATE_LINE_WINDOW = 600.0
STATS_CACHE_SECONDS = 10.0  # docker stats shared by the panel, Info, /statut and /stats


class Busy(Exception):
    """A server operation is already in progress."""


class WardenBot(discord.Client):
    def __init__(self, config: Config, docker_client: Any = None) -> None:
        intents = discord.Intents.none()
        intents.guilds = True  # enough: the member's roles come with every interaction
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.state = State(config.state_dir / "state.json", config.scheduled_restart_default_time)
        self.docker = IcarusDocker(config.container_name, config.icarus_dir, docker_client)
        self.classifier = EventClassifier(config.clean_exit_codes)
        self.patterns = LogPatterns.load(config.log_patterns_file)
        self.notifier = Notifier(self, config, self.state)
        self.pending = Pending()

        self.current_operation: str | None = None
        self._op_lock = asyncio.Lock()
        self.players: pl.Snapshot | None = None
        self._rendered: dict[str, int] = {}
        self._panel_locks: dict[str, asyncio.Lock] = {}
        self._schedule_run: sch.Run | None = None
        self._build_changed_at: float | None = None
        self._stats_lock = asyncio.Lock()
        self._stats_cache: tuple[float, Stats | None] | None = None
        self._presence: str | None = None
        self._started = False
        self._closing = threading.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_again = False

    # ================================================================== start-up
    async def setup_hook(self) -> None:
        from . import commands

        commands.setup(self)
        guild = discord.Object(self.config.guild_id)
        synced = await self.tree.sync(guild=guild)
        log.info("%d slash commands synchronisées sur le serveur %s", len(synced), self.config.guild_id)

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (%s)", self.user, self.user.id if self.user else "?")
        if self._started:
            return  # reconnection: the loops are already running
        self._started = True
        app_id = self.application_id
        log.info(
            "Lien d'invitation : https://discord.com/oauth2/authorize?client_id=%s&scope=bot+applications.commands&permissions=%s",
            app_id,
            INVITE_PERMISSIONS,
        )
        await self.refresh_all_panels(force=True)
        await self._post_startup_message()
        for coro in (
            self._panel_loop(),
            self._player_loop(),
            self._events_loop(),
            self._logs_loop(),
            self._log_flush_loop(),
            self._scheduler_loop(),
        ):
            self._tasks.append(asyncio.create_task(coro))

    async def close(self) -> None:
        self._closing.set()
        for task in self._tasks:
            task.cancel()
        await self.notifier.close()
        await super().close()

    async def _post_startup_message(self) -> None:
        status = await asyncio.to_thread(self.docker.status)
        compose_text = await asyncio.to_thread(self._read_compose)
        locked = [d.key for d in ss.SETTINGS if d.key not in ss.editable_keys(compose_text)]
        lines = [
            f"🤖 **warden-icarus {__version__} démarré**",
            f"Conteneur `{self.config.container_name}` : **{status.state if status.exists else 'introuvable'}**",
            f"ntfy : {'configuré' if self.config.ntfy_enabled else 'non configuré'}",
        ]
        if locked:
            lines.append(
                "⚠️ Réglages écrits en dur dans docker-compose.yml (non modifiables depuis Discord) : "
                + ", ".join(f"`{k}`" for k in locked)
            )
        await self.notifier.post("\n".join(lines))

    # ================================================================== permissions
    def is_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        return (
            interaction.guild_id == self.config.guild_id
            and isinstance(member, discord.Member)
            and any(role.id == self.config.admin_role_id for role in member.roles)
        )

    async def require_admin(self, interaction: discord.Interaction, action: str) -> bool:
        if self.is_admin(interaction):
            return True
        await ui.respond(interaction, f"🔒 Action réservée au rôle <@&{self.config.admin_role_id}>.", ui.Colors.DANGER)
        await self.notifier.denied(interaction.user, action)
        return False

    # ================================================================== interactions
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return  # slash commands: CommandTree; modals: Modal classes
        custom_id = str((interaction.data or {}).get("custom_id", ""))
        if not custom_id.startswith("wi:"):
            return
        if interaction.guild_id != self.config.guild_id:
            return
        from . import handlers

        try:
            await handlers.dispatch(self, interaction, custom_id)
        except Exception:
            log.exception("Erreur lors du traitement de %s", custom_id)
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("❌ Erreur interne, voir les logs du bot.", ephemeral=True)
                else:
                    await interaction.response.send_message("❌ Erreur interne, voir les logs du bot.", ephemeral=True)
            except discord.HTTPException:
                pass

    # ================================================================== server operations
    @asynccontextmanager
    async def operation(self, name: str) -> AsyncIterator[None]:
        """One operation at a time; the Docker events it produces are "expected"."""
        if self._op_lock.locked():
            raise Busy(self.current_operation or "une opération")
        async with self._op_lock:
            self.current_operation = name
            self.classifier.begin_operation(name)
            self.schedule_panel_refresh()
            try:
                yield
            finally:
                self.current_operation = None
                self.classifier.end_operation()
                self.schedule_panel_refresh()

    async def _wait_state(self, running: bool, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = await asyncio.to_thread(self.docker.status)
            if status.running == running:
                return True
            await asyncio.sleep(1)
        return False

    async def server_action(self, action: str) -> tuple[bool, str]:
        """start / stop / restart / update. Return (success, message). Raise Busy."""
        labels = {"start": "Démarrage", "stop": "Arrêt", "restart": "Redémarrage", "update": "Mise à jour"}
        async with self.operation(labels[action]):
            status = await asyncio.to_thread(self.docker.status)
            if not status.exists:
                return False, f"❌ Conteneur `{self.config.container_name}` introuvable."
            try:
                if action == "start":
                    if status.running:
                        return True, "ℹ️ Le serveur est déjà démarré."
                    await asyncio.to_thread(self.docker.start)
                    ok = await self._wait_state(True)
                    return ok, (
                        "✅ Conteneur démarré. Icarus vérifie les mises à jour puis charge le monde : "
                        "comptez quelques minutes avant de pouvoir rejoindre."
                        if ok
                        else "⚠️ Démarrage demandé, mais le conteneur n'est pas encore actif."
                    )
                if action == "stop":
                    if not status.running:
                        return True, "ℹ️ Le serveur est déjà arrêté."
                    await asyncio.to_thread(self.docker.stop, self.config.stop_timeout)
                    return True, "✅ Serveur arrêté (le jeu a eu le temps de sauvegarder la partie)."
                if action == "restart":
                    if not status.running:
                        return False, "⚠️ Le serveur est arrêté : utilise **Démarrer**."
                    await asyncio.to_thread(self.docker.restart, self.config.stop_timeout)
                    return True, "✅ Serveur redémarré. Comptez quelques minutes avant de pouvoir rejoindre."
                if action == "update":
                    if not status.running:
                        return False, (
                            "⚠️ Le serveur est arrêté : utilise **Démarrer**, l'image vérifie les mises à jour "
                            "à chaque démarrage."
                        )
                    output = await asyncio.to_thread(self.docker.force_update)
                    return True, (
                        f"✅ Mise à jour forcée lancée (`{output or 'icarus-force-update: started'}`).\n"
                        "L'image réinstalle Icarus puis relance le jeu : le serveur est indisponible pendant "
                        "le téléchargement. Un message « Mise à jour » sera publié quand le nouveau build sera installé."
                    )
            except Exception as e:  # noqa: BLE001 - DockerError, APIError, requests…
                log.exception("Échec de l'action %s", action)
                return False, f"❌ Échec : {e}"
        return False, "Action inconnue."

    # ================================================================== information
    async def server_info(self, with_stats: bool = True) -> ServerInfo:
        status: ContainerStatus = await asyncio.to_thread(self.docker.status)
        stats = await self._cached_stats() if with_stats and status.running else None
        players = self.players if status.running else None
        name = (
            (players.server_name if players else None)
            or status.env.get("SERVER_NAME")
            or status.env.get("SERVERNAME")  # old image (nerodon)
            or "Serveur Icarus"
        )
        scheduled = self.state.scheduled_restart_time if self.state.scheduled_restart_enabled else None
        return ServerInfo(
            status=status,
            stats=stats,
            players=players,
            operation=self.current_operation,
            server_name=name,
            scheduled_time=scheduled,
            now=datetime.now(self.config.timezone),
            tz=self.config.timezone,
            refresh_seconds=self.config.panel_refresh_seconds,
            buildid=await asyncio.to_thread(read_buildid, self.config.game_dir),
        )

    async def _cached_stats(self) -> Stats | None:
        """`docker stats` (about 2 s, blocks a worker thread), at most once every STATS_CACHE_SECONDS.

        Info, /statut and /stats are open to every member: without this, repeated clicks would
        stack calls and could saturate the thread pool used for every Docker operation.
        """
        async with self._stats_lock:
            cached = self._stats_cache
            if cached is not None and time.monotonic() - cached[0] < STATS_CACHE_SECONDS:
                return cached[1]
            try:
                stats: Stats | None = await asyncio.to_thread(self.docker.stats)
            except Exception as e:  # noqa: BLE001
                log.debug("docker stats unavailable: %s", e)
                stats = None
            self._stats_cache = (time.monotonic(), stats)
            return stats

    def _read_compose(self) -> str:
        try:
            return self.config.icarus_compose_file.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("docker-compose.yml d'Icarus illisible : %s", e)
            return ""

    async def settings_info(self) -> SettingsInfo:
        def gather() -> tuple[dict[str, str], bool, dict[str, str], bool, str]:
            try:
                env, env_ok = ss.read_env(self.config.icarus_env_file), True
            except OSError as e:
                log.warning(".env d'Icarus illisible : %s", e)
                env, env_ok = {}, False
            try:
                ini_file, data_dir = self.config.server_settings_file, self.config.data_dir
                ini, ini_ok = ss.read_ini(ini_file, root=data_dir), safefs.lexists(ini_file, data_dir)
            except (OSError, UnicodeError) as e:
                log.warning("ServerSettings.ini illisible : %s", e)
                ini, ini_ok = {}, False
            return env, env_ok, ini, ini_ok, self._read_compose()

        env_values, env_ok, ini_values, ini_ok, compose_text = await asyncio.to_thread(gather)
        status = await asyncio.to_thread(self.docker.status)
        editable = ss.editable_keys(compose_text)
        pending = ss.pending_keys(env_values, status.env, editable) if status.exists else set()
        return SettingsInfo(
            alerts={k: self.state.alert_enabled(k) for k in ALERT_KEYS},
            ntfy_configured=self.config.ntfy_enabled,
            scheduled_enabled=self.state.scheduled_restart_enabled,
            scheduled_time=self.state.scheduled_restart_time,
            tz_name=str(self.config.timezone),
            env_values=ss.current_values(env_values, ini_values),
            editable=editable,
            pending=pending,
            env_readable=env_ok,
            ini_readable=ini_ok,
        )

    def _backups_data(self) -> tuple[list[bk.Prospect], list[tuple[str, datetime, int]]]:
        tz = self.config.timezone
        prospects = bk.list_prospects(self.config.prospects_dir, tz)
        archives = []
        for path in bk.list_archives(self.config.backup_dir):
            st = path.stat()
            archives.append((path.name, datetime.fromtimestamp(st.st_mtime, tz), st.st_size))
        return prospects, archives

    # ================================================================== panels
    async def _ensure_panel(self, name: str, channel_id: int, spec: ui.MessageSpec, force: bool = False) -> None:
        """Edit the panel message, or create it if missing (without touching other messages)."""
        lock = self._panel_locks.setdefault(name, asyncio.Lock())
        async with lock:
            digest = hash(spec)
            if not force and self._rendered.get(name) == digest:
                return
            channel = self.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                log.warning("Salon %s introuvable pour le panneau %s", channel_id, name)
                return
            ref = self.state.panel_message(name)
            if ref and ref[0] == channel_id:
                try:
                    await channel.get_partial_message(ref[1]).edit(view=ui.render(spec))
                    self._rendered[name] = digest
                    return
                except discord.NotFound:
                    log.info("Message du panneau %s disparu : nouvel envoi", name)
                except discord.HTTPException as e:
                    log.error("Échec de mise à jour du panneau %s : %s", name, e)
                    return
            msg = await channel.send(view=ui.render(spec))
            self.state.set_panel_message(name, channel_id, msg.id)
            self._rendered[name] = digest
            await self._cleanup_own_messages(channel, keep=msg.id)

    async def _cleanup_own_messages(self, channel: discord.TextChannel, keep: int) -> None:
        """When a panel is created: remove the older messages *from this bot* (old panels)."""
        try:
            async for old in channel.history(limit=50):
                if old.id != keep and self.user and old.author.id == self.user.id:
                    await old.delete()
        except discord.HTTPException as e:
            log.info("Nettoyage des anciens panneaux impossible dans %s : %s", channel.id, e)

    async def refresh_server_panel(self, force: bool = False) -> ServerInfo:
        info = await self.server_info()
        await self._ensure_panel("panel", self.config.channel_panel_id, server_panel(info), force)
        await self._update_presence(info)
        return info

    async def refresh_backups_panel(self, force: bool = False) -> None:
        prospects, archives = await asyncio.to_thread(self._backups_data)
        spec = backups_panel(prospects, archives, self.config.backup_retention, self.config.timezone)
        await self._ensure_panel("backups", self.config.channel_backups_id, spec, force)

    async def refresh_settings_panel(self, force: bool = False) -> ui.MessageSpec:
        spec = settings_panel(await self.settings_info())
        await self._ensure_panel("settings", self.config.channel_settings_id, spec, force)
        return spec

    def mark_rendered(self, name: str, spec: ui.MessageSpec) -> None:
        self._rendered[name] = hash(spec)

    async def refresh_all_panels(self, force: bool = False) -> None:
        for refresh in (self.refresh_server_panel, self.refresh_backups_panel, self.refresh_settings_panel):
            try:
                await refresh(force)
            except Exception:
                log.exception("Échec du rafraîchissement d'un panneau")

    def schedule_panel_refresh(self) -> None:
        """Refresh the server panel without blocking the caller (event, start/end of an action)."""
        if not self.is_ready():
            return
        self._refresh_again = True
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_until_clean())

    async def _refresh_until_clean(self) -> None:
        # A request arriving during a refresh triggers another one (final state guaranteed).
        while self._refresh_again:
            self._refresh_again = False
            await self._safe(self.refresh_server_panel)

    async def _safe(self, func: Callable[[], Any]) -> None:
        try:
            await func()
        except Exception:
            log.exception("Erreur dans une tâche de fond")

    async def _update_presence(self, info: ServerInfo) -> None:
        if info.operation:
            text = f"Icarus · {info.operation.lower()}…"
        elif info.status.running and info.players:
            text = f"Icarus · {info.players.summary}"
        elif info.status.running:
            text = "Icarus · démarrage"
        else:
            text = "Icarus · serveur arrêté"
        if text != self._presence:
            self._presence = text
            await self.change_presence(activity=discord.Game(text))

    # ================================================================== loops
    async def _panel_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.panel_refresh_seconds)
            await self.refresh_all_panels()
            await self._safe(self._check_buildid)
            try:
                HEARTBEAT_FILE.write_text(str(time.time()))
            except OSError:
                pass

    async def _check_buildid(self) -> bool:
        """Publish "Update installed" if the build changed. Return True in that case."""
        buildid = await asyncio.to_thread(read_buildid, self.config.game_dir)
        if not buildid or buildid == self.state.last_buildid:
            return False
        previous = self.state.last_buildid
        self.state.set_last_buildid(buildid)
        if not previous:
            return False
        self._build_changed_at = time.monotonic()
        await self.notifier.alert(
            "update",
            "⬆️ Mise à jour d'Icarus installée",
            f"Build **{previous}** → **{buildid}**.",
            ui.Colors.BRAND,
            tags="arrow_up",
        )
        return True

    async def _real_update_happened(self) -> bool:
        """True if the build just changed (here, or during the last pass of the panel loop)."""
        if await self._check_buildid():
            return True
        changed_at = self._build_changed_at
        return changed_at is not None and time.monotonic() - changed_at < UPDATE_LINE_WINDOW

    async def _player_loop(self) -> None:
        previous: pl.Snapshot | None = None
        while True:
            try:
                status = await asyncio.to_thread(self.docker.status)
                if not status.running:
                    previous, self.players = None, None
                else:
                    snap = await pl.query(self.config.a2s_host, self.config.a2s_port)
                    changed = (snap is None) != (self.players is None) or (
                        snap is not None and self.players is not None and snap.count != self.players.count
                    )
                    self.players = snap
                    if snap is not None:
                        if self.config.player_detection in ("a2s", "both"):
                            for _kind, text in pl.diff(previous, snap):
                                await self.notifier.post(text)
                        previous = snap
                    if changed:
                        self.schedule_panel_refresh()
            except Exception:
                log.exception("Erreur dans la boucle joueurs")
            await asyncio.sleep(self.config.player_poll_seconds)

    def _pump(self, name: str, source: Callable[[], Iterator[Any]], queue: asyncio.Queue[Any]) -> None:
        """Read a blocking Docker stream in a thread and push each item into the queue."""
        loop = asyncio.get_running_loop()

        def run() -> None:
            while not self._closing.is_set():
                try:
                    for item in source():
                        loop.call_soon_threadsafe(queue.put_nowait, item)
                        if self._closing.is_set():
                            return
                except Exception as e:  # noqa: BLE001 - socket closed, container missing...
                    log.debug("Flux %s interrompu : %s", name, e)
                self._closing.wait(5)

        threading.Thread(target=run, name=f"docker-{name}", daemon=True).start()

    async def _events_loop(self) -> None:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pump("events", self.docker.events, queue)
        while True:
            event = await queue.get()
            try:
                await self._handle_event(event)
            except Exception:
                log.exception("Erreur de traitement d'un événement Docker")

    async def _handle_event(self, event: dict[str, Any]) -> None:
        result = self.classifier.classify(event)
        log.debug("Événement %s → %s", event.get("Action"), result)
        self.schedule_panel_refresh()
        if result is None:
            return
        if result.kind == CRASH:
            detail = " — **mémoire insuffisante (OOM)**" if result.oom else ""
            await self.notifier.alert(
                "crash",
                "💥 Crash du serveur Icarus",
                f"Le conteneur s'est arrêté avec le code **{result.exit_code}**{detail}.\n"
                "Docker va le relancer automatiquement (restart: unless-stopped).",
                ui.Colors.DANGER,
                priority="urgent",
                tags="boom",
            )
        elif result.kind == RESTART_AFTER_CRASH:
            await self.notifier.alert(
                "unexpected_restart",
                "🔁 Serveur relancé après le crash",
                "Docker a relancé le conteneur. Le jeu met quelques minutes à redevenir disponible.",
                ui.Colors.WARNING,
                priority="high",
                tags="repeat",
            )
        elif result.kind == UNEXPECTED_RESTART:
            await self.notifier.alert(
                "unexpected_restart",
                "🔁 Redémarrage inattendu",
                "Le conteneur a été redémarré en dehors de Discord (docker restart, politique de redémarrage…).",
                ui.Colors.WARNING,
                priority="high",
                tags="repeat",
            )
        elif result.kind == MANUAL_STOP:
            await self.notifier.post("⏹️ Serveur arrêté en dehors de Discord (docker stop/kill).")
        elif result.kind == EXTERNAL_START:
            await self.notifier.post("▶️ Serveur démarré en dehors de Discord.")

    async def _logs_loop(self) -> None:
        queue: asyncio.Queue[str] = asyncio.Queue()
        since = {"t": int(time.time())}

        def source() -> Iterator[str]:
            status = self.docker.status()
            if not status.running:
                return iter(())
            start = since["t"]

            def follow() -> Iterator[str]:
                try:
                    yield from self.docker.logs(start)
                finally:
                    since["t"] = int(time.time())

            return follow()

        self._pump("logs", source, queue)
        while True:
            line = await queue.get()
            try:
                await self._handle_log_line(line)
            except Exception:
                log.exception("Erreur de traitement d'une ligne de log")

    async def _handle_log_line(self, line: str) -> None:
        match = self.patterns.classify(line)
        if match is None or not should_publish(match.category, self.config.error_logs_to_discord):
            return
        if match.category in ("join", "leave"):
            if self.config.player_detection in ("logs", "both"):
                who = f"**{ui.md(match.name)}**" if match.name else "Un joueur"
                verb = "a rejoint" if match.category == "join" else "a quitté"
                await self.notifier.post(f"{LABELS[match.category]} {who} {verb} la partie")
            return
        if match.category == "update" and not await self._real_update_happened():
            return  # "already up to date", or reinstall of the same build
        await self.notifier.queue_log_line(f"{LABELS.get(match.category, '•')} {match.line}")

    async def recent_logs(self, count: int) -> list[str]:
        """Last N lines of the container, normalised (for /logs)."""
        raw = await asyncio.to_thread(self.docker.tail, count)
        lines = (normalize_line(line) for line in raw)
        return [line.display for line in lines if line.text]

    async def _log_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.notifier.flush_log_lines()
            except Exception:
                log.exception("Erreur d'envoi des logs")

    async def _scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._scheduler_tick()
            except Exception:
                log.exception("Erreur du redémarrage planifié")

    async def _scheduler_tick(self) -> None:
        now = datetime.now(self.config.timezone)
        at = sch.parse_hhmm(self.state.scheduled_restart_time)
        target = sch.next_target(now, at)
        if self.current_operation and now >= target:
            return  # an action is in progress: retry on the next pass
        status = await asyncio.to_thread(self.docker.status)
        count: int | None = None
        if status.running and now >= target:
            snap = await pl.query(self.config.a2s_host, self.config.a2s_port)
            count = snap.count if snap else None
        decision = sch.decide(
            now,
            self.state.scheduled_restart_enabled,
            at,
            self._schedule_run,
            status.running,
            count,
            self.config.scheduled_restart_if_a2s_down,
        )
        self._schedule_run = decision.run
        run = decision.run
        actor = "⏰ Redémarrage planifié"
        if decision.action == sch.Action.ANNOUNCE:
            await self.notifier.post(
                f"⏰ **Redémarrage planifié à {self.state.scheduled_restart_time}** (dans 5 min).\n"
                "Il sera reporté si des joueurs sont connectés.",
                ui.Colors.WARNING,
            )
        elif decision.action == sch.Action.RESTART:
            try:
                ok, result = await self.server_action("restart")
            except Busy as e:
                ok, result = False, f"⚠️ Annulé : {e} en cours."
            await self.notifier.action(actor, "redémarrage quotidien", result, ui.Colors.SUCCESS if ok else ui.Colors.DANGER)
        elif decision.action == sch.Action.POSTPONE and run is not None and run.attempts == 1:
            reason = f"{decision.players} joueur(s) connecté(s)" if decision.players else "nombre de joueurs inconnu (A2S sans réponse)"
            await self.notifier.post(
                f"⏸️ Redémarrage planifié **reporté** : {reason}. Nouvel essai toutes les 15 min "
                f"jusqu'à {run.deadline:%H:%M}.",
                ui.Colors.WARNING,
            )
        elif decision.action == sch.Action.GIVE_UP and run is not None:
            await self.notifier.post(
                f"❌ Redémarrage planifié **abandonné** pour aujourd'hui : joueurs encore connectés à {run.deadline:%H:%M}.",
                ui.Colors.DANGER,
            )
        elif decision.action == sch.Action.SKIP_STOPPED:
            log.info("Redémarrage planifié ignoré : serveur arrêté")

    # ================================================================== utilities
    def disk_usage(self) -> tuple[int, int, int] | None:
        try:
            usage = shutil.disk_usage(self.config.icarus_dir)
            return usage.total, usage.used, usage.free
        except OSError:
            return None

    def backups_size(self) -> tuple[int, int]:
        archives = bk.list_archives(self.config.backup_dir)
        return sum(p.stat().st_size for p in archives), len(archives)


__all__ = ["WardenBot", "Busy", "DockerError"]
