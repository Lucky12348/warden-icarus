"""Publication dans le salon logs (actions, alertes, lignes de logs) et sur ntfy."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
import discord

from . import ui
from .antispam import AntiSpam

if TYPE_CHECKING:
    from .config import Config
    from .state import State

log = logging.getLogger(__name__)

MAX_BLOCK = 1800  # caractères de logs par message (limite Discord confortable)


class Notifier:
    def __init__(self, client: discord.Client, config: "Config", state: "State") -> None:
        self.client = client
        self.config = config
        self.state = state
        self._session: aiohttp.ClientSession | None = None
        # Lignes de logs : dédoublonnage + débit maximal par minute.
        self.log_spam = AntiSpam(config.log_max_per_minute, 60.0, config.log_dedupe_seconds)
        # Alertes (crash en boucle…) : 5 par 10 min, même type au plus toutes les 2 min.
        self.alert_spam = AntiSpam(5, 600.0, 120.0)
        # Refus de permission : évite qu'un clic répété inonde le salon.
        self.denied_spam = AntiSpam(10, 600.0, 60.0)
        self._pending_lines: list[str] = []
        self._lines_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    def _channel(self) -> discord.abc.Messageable | None:
        channel = self.client.get_channel(self.config.channel_logs_id)
        if channel is None:
            log.warning("Salon logs %s introuvable", self.config.channel_logs_id)
        return channel  # type: ignore[return-value]

    async def post(self, text: str, color: int = ui.Colors.NEUTRAL, mention_admins: bool = False) -> None:
        channel = self._channel()
        if channel is None:
            return
        allowed = discord.AllowedMentions.none()
        if mention_admins:
            text = f"<@&{self.config.admin_role_id}>\n{text}"
            allowed = discord.AllowedMentions(roles=[discord.Object(self.config.admin_role_id)])
        try:
            await channel.send(view=ui.render(ui.message(text, color)), allowed_mentions=allowed)
        except discord.HTTPException as e:
            log.error("Échec d'envoi dans le salon logs : %s", e)

    # --- journal des actions (qui a fait quoi) ---
    async def action(self, user: discord.abc.User | str, action: str, result: str, color: int = ui.Colors.NEUTRAL) -> None:
        who = user if isinstance(user, str) else f"{user.display_name} (`{user.id}`)"
        log.info("Action %s par %s : %s", action, who, result.replace("\n", " "))
        await self.post(f"👤 **{who}** — {action}\n\n{result}", color)

    async def denied(self, user: discord.abc.User, action: str) -> None:
        log.warning("Action refusée %s pour %s (%s)", action, user, user.id)
        if self.denied_spam.allow(f"{user.id}:{action}", key=f"{user.id}:{action}"):
            await self.post(f"🚫 **{user.display_name}** (`{user.id}`) a tenté : {action} — refusé (rôle admin requis)")

    # --- alertes ---
    async def alert(self, category: str, title: str, body: str, color: int, priority: str = "default", tags: str = "") -> None:
        """`category` : interrupteur concerné (crash, unexpected_restart, update)."""
        if not self.state.alert_enabled(category):
            log.info("Alerte %s désactivée : %s", category, title)
            return
        if not self.alert_spam.allow(title, key=category):
            log.warning("Alerte %s limitée par l'anti-spam : %s", category, title)
            return
        suppressed = self.alert_spam.pop_suppressed()
        if suppressed:
            body += f"\n-# {suppressed} alerte(s) précédente(s) non publiée(s) (anti-spam)"
        if self.state.alert_enabled("discord"):
            await self.post(f"## {title}\n{body}", color, mention_admins=True)
        if self.state.alert_enabled("ntfy"):
            await self.ntfy(title, body, priority, tags)

    async def ntfy(self, title: str, message: str, priority: str = "default", tags: str = "") -> None:
        if not self.config.ntfy_enabled:
            return
        payload = {
            "topic": self.config.ntfy_topic,
            "title": title,
            "message": discord.utils.remove_markdown(message),
            "priority": {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}.get(priority, 3),
        }
        if tags:
            payload["tags"] = [t for t in tags.split(",") if t]
        headers = {}
        if self.config.ntfy_token:
            headers["Authorization"] = f"Bearer {self.config.ntfy_token}"
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
            # Publication JSON sur l'URL racine : pas de souci d'encodage des en-têtes (accents).
            async with self._session.post(self.config.ntfy_url, json=payload, headers=headers) as resp:
                if resp.status >= 300:
                    log.error("ntfy a répondu %s : %s", resp.status, (await resp.text())[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.error("Échec de publication ntfy : %s", e)

    # --- lignes de logs du conteneur (regroupées) ---
    async def queue_log_line(self, line: str) -> None:
        if not self.log_spam.allow(line):
            return
        async with self._lines_lock:
            self._pending_lines.append(line)

    async def flush_log_lines(self) -> None:
        async with self._lines_lock:
            lines, self._pending_lines = self._pending_lines, []
        suppressed = self.log_spam.pop_suppressed()
        if not lines and not suppressed:
            return
        chunks: list[str] = []
        current = ""
        for line in lines:
            line = line.replace("```", "'''")[:400]
            if current and len(current) + len(line) + 1 > MAX_BLOCK:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)
        for i, chunk in enumerate(chunks):
            text = f"📜 **Logs Icarus**\n```\n{chunk.rstrip()}\n```"
            if suppressed and i == len(chunks) - 1:
                text += f"\n-# {suppressed} ligne(s) ignorée(s) par l'anti-spam"
            await self.post(text)
        if suppressed and not chunks:
            await self.post(f"-# 📜 {suppressed} ligne(s) de logs ignorée(s) par l'anti-spam")
