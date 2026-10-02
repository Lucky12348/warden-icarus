"""Gestion des clics (boutons, menus) et des modals.

Format des custom_id : `wi:<zone>:<action>[:<jeton>]`, zones `srv` (panneau serveur),
`bak` (sauvegardes) et `set` (réglages). Lecture seule pour tous (Infos, Actualiser) ;
tout le reste exige le rôle admin, vérifié à chaque étape (y compris à la confirmation).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from . import backups as bk
from . import settings_store as ss
from .panels import info_text, restore_point_label, server_panel
from .scheduler import parse_hhmm
from .state import ALERT_KEYS, ALERT_LABELS
from .ui import ButtonSpec, Colors, OptionSpec, SelectSpec, respond, update

if TYPE_CHECKING:
    from .bot import WardenBot

log = logging.getLogger(__name__)

SERVER_ACTIONS = {
    "start": ("démarrage", "Démarrer"),
    "stop": ("arrêt", "Arrêter"),
    "restart": ("redémarrage", "Redémarrer"),
    "update": ("mise à jour (redémarrage)", "Mettre à jour"),
}

def _cancel(zone: str) -> ButtonSpec:
    return ButtonSpec("Annuler", f"wi:{zone}:cancel", "secondary")


def _values(interaction: discord.Interaction) -> list[str]:
    return list((interaction.data or {}).get("values", []))


async def dispatch(bot: "WardenBot", interaction: discord.Interaction, custom_id: str) -> None:
    parts = custom_id.split(":")
    zone, action = parts[1], parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else ""
    if zone == "srv":
        await server_component(bot, interaction, action, arg)
    elif zone == "bak":
        await backups_component(bot, interaction, action, arg)
    elif zone == "set":
        await settings_component(bot, interaction, action, arg)


async def _expired(interaction: discord.Interaction) -> None:
    await update(interaction, "⌛ Cette sélection a expiré (ou le bot a redémarré). Recommence depuis le panneau.", Colors.NEUTRAL)


# ====================================================================== serveur
async def server_component(bot: "WardenBot", interaction: discord.Interaction, action: str, arg: str) -> None:
    if action == "info":
        await respond(interaction, "⏳ Récupération des informations…", Colors.NEUTRAL)
        info = await bot.server_info()
        await update(interaction, info_text(info, info.status.env.get("PORT", "32768"), bot.config.a2s_port), Colors.BRAND)
        return
    if action == "cancel":
        await update(interaction, "❎ Annulé.", Colors.NEUTRAL)
        return
    if action == "confirm":
        if not await bot.require_admin(interaction, "confirmation d'action serveur"):
            return
        data = bot.pending.pop(arg, interaction.user.id)
        if data is None:
            await _expired(interaction)
            return
        await run_server_action(bot, interaction, data["action"], edit=True)
        return
    if action in SERVER_ACTIONS:
        await request_server_action(bot, interaction, action)


async def request_server_action(bot: "WardenBot", interaction: discord.Interaction, action: str) -> None:
    """Point d'entrée commun aux boutons et aux slash commands."""
    label, button = SERVER_ACTIONS[action]
    if not await bot.require_admin(interaction, label):
        return
    if bot.current_operation:
        await respond(interaction, f"⏳ Une opération est déjà en cours : **{bot.current_operation}**.", Colors.WARNING)
        return
    players = bot.players
    if action != "start" and players is not None and players.count > 0:
        token = bot.pending.put(interaction.user.id, action=action)
        await respond(
            interaction,
            f"# ⚠️ {players.summary} connecté(s)\n\nConfirmer le **{label}** ? Les joueurs seront déconnectés "
            "(la partie est sauvegardée à l'arrêt).",
            Colors.WARNING,
            (ButtonSpec(f"Confirmer : {button}", f"wi:srv:confirm:{token}", "danger"), _cancel("srv")),
        )
        return
    await run_server_action(bot, interaction, action, edit=False)


async def run_server_action(bot: "WardenBot", interaction: discord.Interaction, action: str, edit: bool) -> None:
    from .bot import Busy

    label, _ = SERVER_ACTIONS[action]
    pending = f"⏳ Commande envoyée — {label} en cours…"
    if action in ("stop", "restart", "update"):
        pending += f"\n-# Arrêt propre : jusqu'à {bot.config.stop_timeout} s pour laisser le jeu sauvegarder."
    if edit:
        await update(interaction, pending, Colors.WARNING)
    else:
        await respond(interaction, pending, Colors.WARNING)
    try:
        ok, result = await bot.server_action(action)
    except Busy as e:
        await update(interaction, f"⏳ Une opération est déjà en cours : **{e}**.", Colors.WARNING)
        return
    color = Colors.SUCCESS if ok else Colors.DANGER
    await update(interaction, result, color)
    await bot.notifier.action(interaction.user, f"Serveur — {label}", result, color)


# ====================================================================== sauvegardes
async def backups_component(bot: "WardenBot", interaction: discord.Interaction, action: str, arg: str) -> None:
    if action == "refresh":
        await interaction.response.defer()  # clic sur le panneau : simple accusé de réception
        await bot.refresh_backups_panel(force=True)
        return
    if action == "cancel":
        await update(interaction, "❎ Annulé.", Colors.NEUTRAL)
        return
    if action == "create":
        await create_backup(bot, interaction)
        return
    if not await bot.require_admin(interaction, "restauration de sauvegarde"):
        return
    if action == "restore":
        await _restore_pick_prospect(bot, interaction)
    elif action == "prospect":
        await _restore_pick_point(bot, interaction)
    elif action == "point":
        await _restore_confirm(bot, interaction, arg)
    elif action == "confirm":
        await _restore_run(bot, interaction, arg)


async def create_backup(bot: "WardenBot", interaction: discord.Interaction) -> None:
    from .bot import Busy

    if not await bot.require_admin(interaction, "sauvegarde manuelle"):
        return
    await respond(interaction, "⏳ Sauvegarde en cours…", Colors.WARNING)
    cfg = bot.config
    try:
        async with bot.operation("Sauvegarde"):
            now = datetime.now(cfg.timezone)
            result = await asyncio.to_thread(bk.create_archive, cfg.saves_root, cfg.backup_dir, cfg.prospects_rel, now)
            removed = await asyncio.to_thread(bk.apply_retention, cfg.backup_dir, cfg.backup_retention)
    except Busy as e:
        await update(interaction, f"⏳ Une opération est déjà en cours : **{e}**.", Colors.WARNING)
        return
    except Exception as e:  # noqa: BLE001
        log.exception("Échec de la sauvegarde")
        text = f"❌ Sauvegarde impossible : {e}"
        await update(interaction, text, Colors.DANGER)
        await bot.notifier.action(interaction.user, "Sauvegarde manuelle", text, Colors.DANGER)
        return
    text = (
        f"✅ Archive `{result.path.name}` créée — {result.files} fichier(s), {bk.human_size(result.size)}.\n"
        f"Parties : {', '.join(result.prospects) or 'aucune'}"
    )
    if removed:
        text += f"\nRétention ({cfg.backup_retention}) : {len(removed)} ancienne(s) archive(s) supprimée(s)."
    for warning in result.warnings:
        text += f"\n⚠️ {warning}"
    await update(interaction, text, Colors.SUCCESS)
    await bot.notifier.action(interaction.user, "Sauvegarde manuelle", text, Colors.SUCCESS)
    await bot.refresh_backups_panel(force=True)


async def _restore_pick_prospect(bot: "WardenBot", interaction: discord.Interaction) -> None:
    prospects = await asyncio.to_thread(bk.list_prospects, bot.config.prospects_dir, bot.config.timezone)
    if not prospects:
        await respond(interaction, "⚠️ Aucune partie trouvée dans le dossier des sauvegardes.", Colors.DANGER)
        return
    from .ui import fmt_dt

    options = tuple(
        OptionSpec(p.name, p.name, f"Modifiée le {fmt_dt(p.modified, bot.config.timezone)} • {bk.human_size(p.size)}")
        for p in prospects[:25]
        if len(p.name) <= 100
    )
    await respond(
        interaction,
        "# ♻️ Restauration\n\nChoisis la **partie** à restaurer.",
        Colors.BRAND,
        (SelectSpec("wi:bak:prospect", "Choisir une partie…", options),),
        (_cancel("bak"),),
    )


async def _restore_pick_point(bot: "WardenBot", interaction: discord.Interaction) -> None:
    values = _values(interaction)
    if not values:
        return
    prospect = values[0]
    cfg = bot.config
    try:
        points = await asyncio.to_thread(
            bk.list_restore_points, cfg.prospects_dir, cfg.backup_dir, prospect, cfg.prospects_rel, cfg.timezone
        )
    except bk.BackupError as e:
        await update(interaction, f"❌ {e}", Colors.DANGER)
        return
    options = tuple(restore_point_label(p, cfg.timezone) for p in points if len(p.token) <= 100)[:25]
    if not options:
        await update(interaction, f"⚠️ Aucune sauvegarde trouvée pour **{prospect}**.", Colors.DANGER)
        return
    token = bot.pending.put(interaction.user.id, prospect=prospect)
    await update(
        interaction,
        f"# ♻️ Restauration\n\nPartie : **{prospect}**\n\nChoisis la **sauvegarde** (la plus récente en premier).",
        Colors.BRAND,
        (SelectSpec(f"wi:bak:point:{token}", "Choisir une sauvegarde…", options),),
        (_cancel("bak"),),
    )


async def _restore_confirm(bot: "WardenBot", interaction: discord.Interaction, token: str) -> None:
    data = bot.pending.pop(token, interaction.user.id)
    values = _values(interaction)
    if data is None or not values:
        await _expired(interaction)
        return
    source_code, _, ref = values[0].partition(":")
    source = {"g": "game", "a": "archive"}.get(source_code)
    if source is None:
        await _expired(interaction)
        return
    prospect = data["prospect"]
    confirm = bot.pending.put(interaction.user.id, prospect=prospect, source=source, ref=ref)
    players = bot.players
    warn = f"\n\n⚠️ **{players.summary} connecté(s)** : ils seront déconnectés." if players and players.count else ""
    await update(
        interaction,
        f"# ⚠️ Confirmation\n\n🗺️ Partie : **{prospect}**\n💾 Sauvegarde : `{ref}`"
        f"{' (archive du bot)' if source == 'archive' else ''}\n\n"
        f"1. ⏹️ Arrêt du serveur (jusqu'à {bot.config.stop_timeout} s)\n"
        f"2. 📄 Copie de l'actuelle `{prospect}.json` en `.pre_restore_<date>`\n"
        "3. ♻️ Remplacement par la sauvegarde choisie (qui est conservée)\n"
        f"4. ▶️ Redémarrage du serveur{warn}",
        Colors.WARNING,
        (
            ButtonSpec("Confirmer la restauration", f"wi:bak:confirm:{confirm}", "danger"),
            _cancel("bak"),
        ),
    )


async def _restore_run(bot: "WardenBot", interaction: discord.Interaction, token: str) -> None:
    from .bot import Busy

    data = bot.pending.pop(token, interaction.user.id)
    if data is None:
        await _expired(interaction)
        return
    prospect, source, ref = data["prospect"], data["source"], data["ref"]
    cfg = bot.config
    action = f"Restauration — {prospect}"
    await update(interaction, "⏹️ Arrêt du serveur avant restauration…", Colors.WARNING)
    try:
        async with bot.operation("Restauration"):
            status = await asyncio.to_thread(bot.docker.status)
            if status.running:
                await asyncio.to_thread(bot.docker.stop, cfg.stop_timeout)
            if (await asyncio.to_thread(bot.docker.status)).running:
                text = "❌ Le serveur ne s'est pas arrêté : restauration annulée."
                await update(interaction, text, Colors.DANGER)
                await bot.notifier.action(interaction.user, action, text, Colors.DANGER)
                return
            try:
                now = datetime.now(cfg.timezone)
                result = await asyncio.to_thread(
                    bk.restore_prospect, cfg.prospects_dir, cfg.backup_dir, prospect, source, ref, cfg.prospects_rel, now
                )
                text = f"✅ `{ref}` restaurée vers `{prospect}.json`."
                if result.pre_restore:
                    text += f"\nAncienne version conservée : `{result.pre_restore.name}`"
                color = Colors.SUCCESS
            except Exception as e:  # noqa: BLE001 - fichiers intacts : on relance quand même
                log.exception("Échec de restauration")
                text, color = f"❌ Restauration impossible : {e}\nLa partie actuelle n'a pas été modifiée.", Colors.DANGER
            await update(interaction, f"{text}\n\n▶️ Redémarrage du serveur…", Colors.WARNING)
            await asyncio.to_thread(bot.docker.start)
            text += "\n▶️ Serveur redémarré."
    except Busy as e:
        await update(interaction, f"⏳ Une opération est déjà en cours : **{e}**.", Colors.WARNING)
        return
    except Exception as e:  # noqa: BLE001
        log.exception("Échec pendant la restauration")
        text, color = f"❌ Erreur : {e}\n👉 Vérifie l'état du serveur et démarre-le si besoin.", Colors.DANGER
    await update(interaction, text, color)
    await bot.notifier.action(interaction.user, action, text, color)
    await bot.refresh_backups_panel(force=True)


# ====================================================================== réglages
async def settings_component(bot: "WardenBot", interaction: discord.Interaction, action: str, arg: str) -> None:
    if action == "cancel":
        await update(interaction, "❎ Annulé, rien n'a été modifié.", Colors.NEUTRAL)
        return
    labels = {
        "alert": "réglage des alertes",
        "sched": "redémarrage planifié",
        "schedtime": "heure du redémarrage planifié",
        "edit": "modification d'un réglage",
        "apply": "application d'un réglage",
        "save": "enregistrement d'un réglage",
        "recreate": "recréation du conteneur",
        "recreate_ok": "recréation du conteneur",
    }
    if not await bot.require_admin(interaction, labels.get(action, action)):
        return

    if action == "alert" and arg in ALERT_KEYS:
        enabled = bot.state.toggle_alert(arg)
        await _refresh_settings_in_place(bot, interaction)
        await bot.notifier.action(interaction.user, f"Alerte « {ALERT_LABELS[arg]} »", "✅ activée" if enabled else "⬜ désactivée")
    elif action == "sched":
        enabled = bot.state.toggle_scheduled_restart()
        await _refresh_settings_in_place(bot, interaction)
        await bot.notifier.action(
            interaction.user,
            "Redémarrage planifié",
            f"✅ activé ({bot.state.scheduled_restart_time})" if enabled else "⬜ désactivé",
        )
        bot.schedule_panel_refresh()
    elif action == "schedtime":
        await interaction.response.send_modal(ScheduleTimeModal(bot))
    elif action == "edit":
        await _open_setting_modal(bot, interaction)
    elif action in ("apply", "save"):
        await _apply_setting(bot, interaction, arg, recreate=action == "apply")
    elif action == "recreate":
        players = bot.players
        warn = f"\n\n⚠️ **{players.summary} connecté(s)** : ils seront déconnectés." if players and players.count else ""
        await respond(
            interaction,
            "# 🔁 Recréer le conteneur ?\n\nLes réglages modifiés du `.env` seront appliqués : arrêt propre, "
            f"puis `docker compose up -d --force-recreate`.{warn}",
            Colors.WARNING,
            (ButtonSpec("Recréer maintenant", "wi:set:recreate_ok", "danger"), _cancel("set")),
        )
    elif action == "recreate_ok":
        await update(interaction, "🔁 Recréation du conteneur en cours…", Colors.WARNING)
        text, color = await _recreate(bot)
        await update(interaction, text, color)
        await bot.notifier.action(interaction.user, "Recréation du conteneur", text, color)


async def _refresh_settings_in_place(bot: "WardenBot", interaction: discord.Interaction) -> None:
    """Le panneau réglages est mis à jour sur place : l'état des interrupteurs est visible par tous."""
    from .panels import settings_panel
    from .ui import render

    spec = settings_panel(await bot.settings_info())
    await interaction.response.edit_message(view=render(spec))
    bot.mark_rendered("settings", spec)


async def _open_setting_modal(bot: "WardenBot", interaction: discord.Interaction) -> None:
    values = _values(interaction)
    defn = ss.SETTINGS_BY_KEY.get(values[0]) if values else None
    if defn is None:
        return
    compose_text = await asyncio.to_thread(bot._read_compose)
    if defn.key not in ss.editable_keys(compose_text):
        await respond(interaction, f"🔒 `{defn.key}` est écrit en dur dans docker-compose.yml.", Colors.DANGER)
        return
    env = await asyncio.to_thread(ss.read_env, bot.config.icarus_env_file)
    await interaction.response.send_modal(SettingModal(bot, defn, env.get(defn.key, "")))


async def _apply_setting(bot: "WardenBot", interaction: discord.Interaction, token: str, recreate: bool) -> None:
    data = bot.pending.pop(token, interaction.user.id)
    if data is None:
        await _expired(interaction)
        return
    key, value, summary = data["key"], data["value"], data["summary"]
    env_file = bot.config.icarus_env_file
    await update(interaction, "💾 Écriture du `.env`…", Colors.WARNING)
    try:
        await asyncio.to_thread(ss.write_env, env_file, {key: value})
        await asyncio.to_thread(bot.docker.compose_check)
    except Exception as e:  # noqa: BLE001
        log.exception("Échec d'écriture du réglage %s", key)
        restored = await asyncio.to_thread(ss.restore_env_backup, env_file)
        text = f"❌ Réglage non appliqué : {e}" + ("\n.env remis dans son état précédent (.env.bak)." if restored else "")
        await update(interaction, text, Colors.DANGER)
        await bot.notifier.action(interaction.user, "Réglages Icarus", f"{summary}\n{text}", Colors.DANGER)
        return
    text = f"✅ {summary}\n.env mis à jour (copie précédente : `.env.bak`)."
    color = Colors.SUCCESS
    if recreate:
        await update(interaction, f"{text}\n\n🔁 Recréation du conteneur…", Colors.WARNING)
        recreate_text, color = await _recreate(bot)
        text += f"\n{recreate_text}"
    else:
        text += "\n⏳ Sera appliqué à la prochaine recréation du conteneur."
    await update(interaction, text, color)
    await bot.notifier.action(interaction.user, "Réglages Icarus", text, color)
    await bot.refresh_settings_panel(force=True)


async def _recreate(bot: "WardenBot") -> tuple[str, int]:
    from .bot import Busy

    try:
        async with bot.operation("Recréation"):
            await asyncio.to_thread(bot.docker.compose_check)
            await asyncio.to_thread(bot.docker.recreate, bot.config.stop_timeout, bot.config.recreate_timeout)
        text, color = "✅ Conteneur recréé avec les nouveaux réglages. Démarrage d'Icarus en cours.", Colors.SUCCESS
    except Busy as e:
        text, color = f"⏳ Une opération est déjà en cours : **{e}**.", Colors.WARNING
    except Exception as e:  # noqa: BLE001
        log.exception("Échec de la recréation")
        text, color = f"❌ Recréation impossible : {e}", Colors.DANGER
    await bot.refresh_settings_panel(force=True)
    return text, color


class SettingModal(discord.ui.Modal):
    def __init__(self, bot: "WardenBot", defn: ss.SettingDef, current: str) -> None:
        super().__init__(title=f"Modifier : {defn.label}"[:45], timeout=600, custom_id=f"wim:set:{defn.key}")
        self.bot = bot
        self.defn = defn
        self.current = current
        if defn.kind == "secret":
            placeholder = "Vide = inchangé · « - » seul = aucun mot de passe"
            default = None
        elif defn.kind == "bool":
            placeholder, default = "True ou False (oui/non acceptés)", current or None
        else:
            placeholder, default = defn.help[:100], current or None
        self.field = discord.ui.TextInput(
            label=defn.label[:45],
            placeholder=placeholder,
            default=default,
            required=defn.kind not in ("secret",),
            max_length=defn.max_length,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.bot.require_admin(interaction, f"modification de {self.defn.key}"):
            return
        raw = self.field.value
        if self.defn.kind == "secret":
            if raw == "":
                await respond(interaction, "ℹ️ Champ vide : mot de passe inchangé.", Colors.NEUTRAL)
                return
            raw = "" if raw.strip() == "-" else raw
        try:
            value = ss.validate(self.defn, raw)
        except ss.SettingError as e:
            await respond(interaction, f"❌ {e}", Colors.DANGER)
            return
        if value == self.current:
            await respond(interaction, "ℹ️ Valeur identique : rien à modifier.", Colors.NEUTRAL)
            return
        before = ss.display_value(self.defn, self.current)
        after = ss.display_value(self.defn, value)
        if self.defn.kind == "secret":
            summary = f"**{self.defn.label}** : modifié" if value else f"**{self.defn.label}** : supprimé"
        else:
            summary = f"**{self.defn.label}** : {before} → {after}"
        token = self.bot.pending.put(interaction.user.id, key=self.defn.key, value=value, summary=summary)
        players = self.bot.players
        warn = f"\n⚠️ **{players.summary} connecté(s)** : ils seront déconnectés." if players and players.count else ""
        await respond(
            interaction,
            f"# ⚙️ Confirmer la modification\n\n{summary}\n\n"
            "Le `.env` d'Icarus sera réécrit (copie `.env.bak`). Le changement ne prend effet qu'après "
            f"**recréation du conteneur** (arrêt propre puis redémarrage).{warn}",
            Colors.WARNING,
            (
                ButtonSpec("Appliquer et recréer", f"wi:set:apply:{token}", "danger"),
                ButtonSpec("Enregistrer seulement", f"wi:set:save:{token}", "primary"),
                _cancel("set"),
            ),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Erreur dans le modal de réglage", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Erreur interne.", ephemeral=True)


class ScheduleTimeModal(discord.ui.Modal):
    def __init__(self, bot: "WardenBot") -> None:
        super().__init__(title="Heure du redémarrage planifié", timeout=600, custom_id="wim:set:schedtime")
        self.bot = bot
        self.field = discord.ui.TextInput(
            label=f"Heure (HH:MM, {bot.config.timezone})"[:45],
            default=bot.state.scheduled_restart_time,
            min_length=4,
            max_length=5,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.bot.require_admin(interaction, "heure du redémarrage planifié"):
            return
        try:
            at = parse_hhmm(self.field.value)
        except ValueError as e:
            await respond(interaction, f"❌ {e}", Colors.DANGER)
            return
        hhmm = f"{at.hour:02d}:{at.minute:02d}"
        self.bot.state.set_scheduled_restart_time(hhmm)
        await respond(interaction, f"✅ Redémarrage planifié à **{hhmm}**.", Colors.SUCCESS)
        await self.bot.refresh_settings_panel(force=True)
        self.bot.schedule_panel_refresh()
        await self.bot.notifier.action(interaction.user, "Heure du redémarrage planifié", f"✅ {hhmm}")


# ====================================================================== statut (slash)
async def status_reply(bot: "WardenBot", interaction: discord.Interaction) -> None:
    await respond(interaction, "⏳ Récupération de l'état…", Colors.NEUTRAL)
    info = await bot.server_info()
    spec = server_panel(info)
    text_blocks = [b for b in spec.blocks if isinstance(b, str)]
    await update(interaction, "\n\n".join(text_blocks), spec.color)
