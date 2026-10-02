"""Slash commands (filet de secours des panneaux) : lecture pour tous, actions réservées au rôle admin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord

from . import handlers
from .panels import stats_text
from .ui import Colors, respond, update

if TYPE_CHECKING:
    from .bot import WardenBot


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

    @tree.command(name="maj", description="Mettre à jour Icarus = redémarrer le conteneur (admin)", guild=guild)
    async def maj(interaction: discord.Interaction) -> None:
        await handlers.request_server_action(bot, interaction, "update")

    @tree.command(name="sauvegarde", description="Créer une archive des parties (admin)", guild=guild)
    async def sauvegarde(interaction: discord.Interaction) -> None:
        await handlers.create_backup(bot, interaction)

    @tree.command(name="panneaux", description="Republier / rafraîchir les panneaux (admin)", guild=guild)
    async def panneaux(interaction: discord.Interaction) -> None:
        if not await bot.require_admin(interaction, "rafraîchissement des panneaux"):
            return
        await respond(interaction, "⏳ Rafraîchissement des panneaux…", Colors.NEUTRAL)
        await bot.refresh_all_panels(force=True)
        await update(interaction, "✅ Panneaux à jour.", Colors.SUCCESS)
        await bot.notifier.action(interaction.user, "Panneaux", "Rafraîchissement forcé")
