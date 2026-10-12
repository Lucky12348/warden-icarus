"""Slash commands (fallback for the panels): read-only for everyone, actions reserved to the admin role."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from . import handlers
from .panels import stats_text
from .ui import Colors, respond, update

if TYPE_CHECKING:
    from .bot import WardenBot

log = logging.getLogger(__name__)

LOGS_MAX_LINES = 2000
MESSAGE_LIMIT = 2000  # characters in a Discord message


def format_logs(lines: list[str]) -> str | None:
    """Code block ready to send, or None if too long for a message (-> .txt file)."""
    if not lines:
        return "ℹ️ Aucune ligne de logs."
    body = "\n".join(lines).replace("```", "'''")
    text = f"```\n{body}\n```"
    return text if len(text) <= MESSAGE_LIMIT else None


def logs_file(lines: list[str], max_bytes: int) -> tuple[bytes, int]:
    """UTF-8 .txt content of at most `max_bytes`, keeping the most recent lines. Return (content, kept)."""
    encoded = [(line + "\n").encode("utf-8") for line in lines]
    total, start = 0, len(encoded)
    while start > 0 and total + len(encoded[start - 1]) <= max_bytes:
        start -= 1
        total += len(encoded[start])
    return b"".join(encoded[start:]), len(encoded) - start


def setup(bot: "WardenBot") -> None:
    guild = discord.Object(bot.config.guild_id)
    tree = bot.tree

    @tree.command(name="aide", description="Liste des commandes du bot Icarus", guild=guild)
    async def aide(interaction: discord.Interaction) -> None:
        admin = bot.is_admin(interaction)
        text = (
            "# 🧊 warden-icarus\n\n"
            "**Pour tous**\n"
            "`/statut` — état du serveur, joueurs, CPU/RAM\n"
            "`/stats` — statistiques détaillées (CPU, RAM, disque, sauvegardes)\n"
            "Bouton **Infos** du panneau\n\n"
            f"**Rôle <@&{bot.config.admin_role_id}>**\n"
            "`/demarrer` · `/arreter` · `/redemarrer` · `/maj` — mêmes actions que les boutons du panneau\n"
            "`/sauvegarde` — archive horodatée des parties\n"
            "`/logs [lignes]` — dernières lignes de logs du serveur (réponse privée)\n"
            "`/panneaux` — republie / rafraîchit les panneaux\n"
            "Restauration et réglages : salons des panneaux sauvegardes et réglages.\n\n"
            + ("✅ Tu as le rôle admin." if admin else "ℹ️ Tu n'as pas le rôle admin : accès en lecture seule.")
        )
        await respond(interaction, text, Colors.BRAND)

    @tree.command(name="statut", description="État du serveur Icarus", guild=guild)
    async def statut(interaction: discord.Interaction) -> None:
        await handlers.status_reply(bot, interaction)

    @tree.command(name="stats", description="Statistiques du serveur Icarus", guild=guild)
    async def stats(interaction: discord.Interaction) -> None:
        await respond(interaction, "⏳ Calcul des statistiques…", Colors.NEUTRAL)
        info = await bot.server_info()
        disk = await asyncio.to_thread(bot.disk_usage)
        size, count = await asyncio.to_thread(bot.backups_size)
        await update(interaction, stats_text(info, disk, size, count), Colors.BRAND)

    @tree.command(name="demarrer", description="Démarrer le serveur Icarus (admin)", guild=guild)
    async def demarrer(interaction: discord.Interaction) -> None:
        await handlers.request_server_action(bot, interaction, "start")

    @tree.command(name="arreter", description="Arrêter le serveur Icarus (admin)", guild=guild)
    async def arreter(interaction: discord.Interaction) -> None:
        await handlers.request_server_action(bot, interaction, "stop")

    @tree.command(name="redemarrer", description="Redémarrer le serveur Icarus (admin)", guild=guild)
    async def redemarrer(interaction: discord.Interaction) -> None:
        await handlers.request_server_action(bot, interaction, "restart")

    @tree.command(name="maj", description="Forcer la mise à jour d'Icarus via l'image (admin)", guild=guild)
    async def maj(interaction: discord.Interaction) -> None:
        await handlers.request_server_action(bot, interaction, "update")

    @tree.command(name="sauvegarde", description="Créer une archive des parties (admin)", guild=guild)
    async def sauvegarde(interaction: discord.Interaction) -> None:
        await handlers.create_backup(bot, interaction)

    @tree.command(name="logs", description="Dernières lignes de logs du serveur Icarus (admin)", guild=guild)
    @app_commands.describe(lignes=f"Nombre de lignes (1 à {LOGS_MAX_LINES}, 50 par défaut)")
    async def logs(interaction: discord.Interaction, lignes: app_commands.Range[int, 1, LOGS_MAX_LINES] = 50) -> None:
        if not await bot.require_admin(interaction, "consultation des logs"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            lines = await bot.recent_logs(lignes)
        except Exception as e:  # noqa: BLE001 - DockerError, APIError...
            await interaction.followup.send(f"❌ Logs indisponibles : {e}", ephemeral=True)
            return
        try:
            text = format_logs(lines)
            if text is not None:
                await interaction.followup.send(text, ephemeral=True)
                return
            limit = (interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024) - 64 * 1024
            payload, kept = logs_file(lines, limit)
            stamp = datetime.now(bot.config.timezone).strftime("%Y%m%d-%H%M%S")
            note = "" if kept == len(lines) else f" (les {len(lines) - kept} plus anciennes retirées : limite d'envoi Discord)"
            await interaction.followup.send(
                f"📜 {kept} dernière(s) ligne(s) de `{bot.config.container_name}`{note}",
                file=discord.File(io.BytesIO(payload), filename=f"icarus-logs-{stamp}.txt"),
                ephemeral=True,
            )
        except discord.HTTPException as e:
            log.exception("Sending /logs failed")
            await interaction.followup.send(f"❌ Envoi impossible : {e}", ephemeral=True)

    @tree.command(name="panneaux", description="Republier / rafraîchir les panneaux (admin)", guild=guild)
    async def panneaux(interaction: discord.Interaction) -> None:
        if not await bot.require_admin(interaction, "rafraîchissement des panneaux"):
            return
        await respond(interaction, "⏳ Rafraîchissement des panneaux…", Colors.NEUTRAL)
        await bot.refresh_all_panels(force=True)
        await update(interaction, "✅ Panneaux à jour.", Colors.SUCCESS)
        await bot.notifier.action(interaction.user, "Panneaux", "Rafraîchissement forcé")
