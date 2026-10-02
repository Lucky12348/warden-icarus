"""Contenu des panneaux persistants (serveur, sauvegardes, réglages) et des réponses Infos/Stats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from . import backups as bk
from . import settings_store as ss
from .docker_ctl import ContainerStatus, Stats
from .players import Snapshot, players_line
from .state import ALERT_KEYS, ALERT_LABELS
from .ui import ButtonSpec, Colors, MessageSpec, OptionSpec, SelectSpec, fmt_bytes, fmt_duration, fmt_dt, message

STARTUP_GRACE = timedelta(minutes=15)  # mise à jour + chargement du monde au démarrage


@dataclass(frozen=True)
class ServerInfo:
    status: ContainerStatus
    stats: Stats | None
    players: Snapshot | None
    operation: str | None
    server_name: str
    scheduled_time: str | None
    now: datetime
    tz: tzinfo
    refresh_seconds: int
    buildid: str | None = None


def status_line(info: ServerInfo) -> tuple[str, int]:
    st = info.status
    if info.operation:
        return f"🔄 **{info.operation} en cours…**", Colors.WARNING
    if not st.exists:
        return "❓ **Conteneur introuvable**", Colors.DANGER
    if st.state == "running":
        uptime = info.now - st.started_at if st.started_at else None
        since = f" — depuis {fmt_duration(uptime)}" if uptime is not None else ""
        if info.players is not None:
            return f"🟢 **En ligne**{since}", Colors.SUCCESS
        if uptime is not None and uptime < STARTUP_GRACE:
            return f"🟡 **Démarrage / mise à jour en cours…**{since}", Colors.WARNING
        return f"🟠 **Conteneur actif, le jeu ne répond pas aux requêtes**{since}", Colors.ORANGE
    if st.state == "restarting":
        return "🔄 **Redémarrage par Docker…**", Colors.WARNING
    if st.state == "paused":
        return "⏸️ **En pause**", Colors.WARNING
    if st.state == "dead":
        return "🔴 **Conteneur en échec**", Colors.DANGER
    code = f" (code de sortie {st.exit_code})" if st.exit_code not in (None, 0, 137, 143) else ""
    return f"⚪ **Arrêté**{code}", Colors.NEUTRAL


def resources_line(stats: Stats | None) -> str:
    if stats is None:
        return "🖥️ CPU — · RAM —"
    cpu = "—" if stats.cpu_percent is None else f"{stats.cpu_percent:.0f} %"
    ram = fmt_bytes(stats.mem_used)
    if stats.mem_limit:
        ram += f" / {fmt_bytes(stats.mem_limit)}"
    return f"🖥️ CPU **{cpu}** · RAM **{ram}**"


def server_panel(info: ServerInfo) -> MessageSpec:
    line, color = status_line(info)
    running = info.status.running and not info.operation
    busy = info.operation is not None
    body = [f"# 🎮 {info.server_name}", line]
    if info.status.running:
        body.append(players_line(info.players))
        body.append(resources_line(info.stats))
    text = "\n".join(body[:1]) + "\n\n" + "\n".join(body[1:])
    if info.scheduled_time:
        text += f"\n⏰ Redémarrage planifié à **{info.scheduled_time}** (reporté si des joueurs sont connectés)"
    text += (
        f"\n\n-# Actualisé à {info.now.astimezone(info.tz):%H:%M} · toutes les {info.refresh_seconds} s"
        " · « Mettre à jour » = redémarrage : l'image installe la dernière version au démarrage."
    )
    buttons = (
        ButtonSpec("Démarrer", "wi:srv:start", "success", disabled=busy or info.status.running, emoji="▶️"),
        ButtonSpec("Arrêter", "wi:srv:stop", "danger", disabled=busy or not running, emoji="⏹️"),
        ButtonSpec("Redémarrer", "wi:srv:restart", "primary", disabled=busy or not running, emoji="🔁"),
        ButtonSpec("Mettre à jour", "wi:srv:update", "secondary", disabled=busy or not info.status.exists, emoji="⬆️"),
        ButtonSpec("Infos", "wi:srv:info", "secondary", emoji="ℹ️"),
    )
    return message(text, color, buttons)


def info_text(info: ServerInfo, game_port: str, query_port: int) -> str:
    st = info.status
    line, _ = status_line(info)
    lines = [f"# ℹ️ {info.server_name}", line]
    if st.running:
        lines.append(players_line(info.players))
        lines.append(resources_line(info.stats))
    lines.append("")
    if st.exists:
        if st.started_at:
            lines.append(f"**Démarré le** : {fmt_dt(st.started_at, info.tz)}")
        if not st.running and st.finished_at:
            lines.append(f"**Arrêté le** : {fmt_dt(st.finished_at, info.tz)} (code {st.exit_code})")
        lines.append(f"**Image** : `{st.image}` · conteneur `{st.short_id}`")
        lines.append(f"**Ports** : {', '.join(st.ports) or f'{game_port}/udp, {query_port}/udp'}")
        lines.append(f"**Relances automatiques (Docker)** : {st.restart_count}")
    if info.buildid:
        lines.append(f"**Build du jeu** : {info.buildid}")
    if info.scheduled_time:
        lines.append(f"**Redémarrage planifié** : tous les jours à {info.scheduled_time}")
    return "\n".join(lines)


def stats_text(info: ServerInfo, disk: tuple[int, int, int] | None, backups_size: int, archives: int) -> str:
    from .ui import progress_bar

    lines = ["# 📊 Statistiques", status_line(info)[0]]
    if info.stats and info.status.running:
        if info.stats.cpu_percent is not None:
            lines.append(f"**CPU (conteneur)** : {progress_bar(info.stats.cpu_percent)}")
        if info.stats.mem_used is not None and info.stats.mem_limit:
            pct = info.stats.mem_used / info.stats.mem_limit * 100
            lines.append(f"**RAM** : {progress_bar(pct)} — {fmt_bytes(info.stats.mem_used)} / {fmt_bytes(info.stats.mem_limit)}")
    if info.status.running:
        lines.append(players_line(info.players))
    if disk:
        total, used, _free = disk
        lines.append(f"**Disque** : {progress_bar(used / total * 100)} — {fmt_bytes(used)} / {fmt_bytes(total)}")
    lines.append(f"**Archives de sauvegarde** : {archives} ({fmt_bytes(backups_size)})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- sauvegardes
def backups_panel(prospects: list[bk.Prospect], archives: list[tuple[str, datetime, int]], retention: int, tz: tzinfo) -> MessageSpec:
    lines = ["# 📦 Sauvegardes", ""]
    if prospects:
        lines.append(f"**Parties** ({len(prospects)})")
        for p in prospects[:10]:
            lines.append(f"• **{p.name}** — modifiée le {fmt_dt(p.modified, tz)} · {bk.human_size(p.size)}")
        if len(prospects) > 10:
            lines.append(f"-# … et {len(prospects) - 10} autre(s)")
    else:
        lines.append("*Aucune partie trouvée.*")
    lines.append("")
    if archives:
        name, modified, size = archives[0]
        lines.append(
            f"**Archives du bot** : {len(archives)} (les {retention} plus récentes sont conservées)\n"
            f"Dernière : `{name}` — {fmt_dt(modified, tz)} · {bk.human_size(size)}"
        )
    else:
        lines.append(f"**Archives du bot** : aucune (rétention : {retention})")
    lines.append(
        "\n-# Restauration : arrêt du serveur → copie de la partie actuelle en `.pre_restore_<date>` "
        "→ remplacement → redémarrage. Les sauvegardes du jeu (`.backup_N`) et les archives sont proposées."
    )
    buttons = (
        ButtonSpec("Sauvegarder maintenant", "wi:bak:create", "primary", emoji="💾"),
        ButtonSpec("Restaurer…", "wi:bak:restore", "danger", emoji="♻️"),
        ButtonSpec("Actualiser", "wi:bak:refresh", "secondary", emoji="🔄"),
    )
    return message("\n".join(lines), Colors.BRAND, buttons)


def restore_point_label(point: bk.RestorePoint, tz: tzinfo) -> OptionSpec:
    if point.kind == "archive":
        label = f"Archive du bot · {fmt_dt(point.modified, tz)}"
    elif point.kind == "pre_restore":
        label = f"Avant restauration · {fmt_dt(point.modified, tz)}"
    else:
        label = f"Sauvegarde du jeu · {point.ref.split('.json.', 1)[-1]}"
    return OptionSpec(label, point.token, f"{fmt_dt(point.modified, tz)} • {bk.human_size(point.size)} • {point.ref}")


# --------------------------------------------------------------------------- réglages
@dataclass(frozen=True)
class SettingsInfo:
    alerts: dict[str, bool]
    ntfy_configured: bool
    scheduled_enabled: bool
    scheduled_time: str
    tz_name: str
    env_values: dict[str, str]
    editable: set[str]
    pending: set[str]
    env_readable: bool


def settings_panel(info: SettingsInfo) -> MessageSpec:
    alert_buttons = tuple(
        ButtonSpec(
            ALERT_LABELS[key] + (" (non configuré)" if key == "ntfy" and not info.ntfy_configured else ""),
            f"wi:set:alert:{key}",
            "success" if info.alerts.get(key) else "secondary",
        )
        for key in ALERT_KEYS
    )
    sched = (
        f"✅ Tous les jours à **{info.scheduled_time}** ({info.tz_name}) — annoncé 5 min avant, "
        "reporté si des joueurs sont connectés (nouvel essai toutes les 15 min, 2 h max)."
        if info.scheduled_enabled
        else f"⬜ Désactivé (heure configurée : {info.scheduled_time})."
    )
    sched_buttons = (
        ButtonSpec("Redémarrage planifié", "wi:set:sched", "success" if info.scheduled_enabled else "secondary", emoji="⏰"),
        ButtonSpec("Changer l'heure", "wi:set:schedtime", "secondary", emoji="🕕"),
    )

    lines = []
    if not info.env_readable:
        lines.append("⚠️ Impossible de lire le `.env` d'Icarus.")
    for defn in ss.SETTINGS:
        marker = ""
        if defn.key not in info.editable:
            marker = " 🔒"
        elif defn.key in info.pending:
            marker = " ⏳"
        lines.append(f"**{defn.label}** : {ss.display_value(defn, info.env_values.get(defn.key))}{marker}")
    legend = "-# ⏳ modifié, appliqué à la prochaine recréation du conteneur"
    if any(d.key not in info.editable for d in ss.SETTINGS):
        legend += " · 🔒 écrit en dur dans docker-compose.yml (non modifiable ici)"

    options = tuple(
        OptionSpec(d.label, d.key, ss.display_value(d, info.env_values.get(d.key)))
        for d in ss.SETTINGS
        if d.key in info.editable
    )
    blocks: list = [
        "# ⚙️ Réglages",
        "## 🔔 Alertes\nVert = activé. « Alertes Discord » et « ntfy » choisissent la destination ; "
        "les autres choisissent les événements signalés.",
        alert_buttons,
        f"## ⏰ Redémarrage planifié\n{sched}",
        sched_buttons,
        "## 🧊 Serveur Icarus\n" + "\n".join(lines) + "\n" + legend,
    ]
    if options:
        blocks.append((SelectSpec("wi:set:edit", "Modifier un réglage…", options),))
    if info.pending:
        blocks.append(
            (ButtonSpec(f"Recréer le conteneur pour appliquer ({len(info.pending)})", "wi:set:recreate", "danger", emoji="🔁"),)
        )
    return MessageSpec(tuple(blocks), Colors.BRAND)
