"""Mise en forme Components V2 (conteneur + barre de couleur + séparateurs), comme l'ancien bot.

Les messages sont d'abord décrits par des « specs » (données pures, comparables) puis rendus
en `discord.ui.LayoutView`. Les vues sont envoyées « arrêtées » : discord.py ne les garde
pas en mémoire et tous les clics passent par le routeur `on_interaction` du bot, ce qui
rend les boutons persistants entre deux redémarrages du bot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

import discord


class Colors:
    BRAND = 0x5865F2
    SUCCESS = 0x57F287
    WARNING = 0xFEE75C
    DANGER = 0xED4245
    NEUTRAL = 0x99AAB5
    ORANGE = 0xE67E22


_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


@dataclass(frozen=True)
class ButtonSpec:
    label: str
    custom_id: str
    style: str = "secondary"
    disabled: bool = False
    emoji: str | None = None


@dataclass(frozen=True)
class OptionSpec:
    label: str
    value: str
    description: str | None = None


@dataclass(frozen=True)
class SelectSpec:
    custom_id: str
    placeholder: str
    options: tuple[OptionSpec, ...]
    disabled: bool = False


Row = tuple  # tuple[ButtonSpec, ...] ou tuple[SelectSpec]


@dataclass(frozen=True)
class MessageSpec:
    """Blocs affichés dans l'ordre, séparés par un séparateur : texte (str) ou rangée de composants."""

    blocks: tuple = field(default_factory=tuple)
    color: int = Colors.BRAND


def message(text: str, color: int = Colors.BRAND, *rows: Row) -> MessageSpec:
    """Texte découpé en paragraphes (ligne vide = séparateur), puis rangées de composants."""
    blocks: list = [b for b in re.split(r"\n{2,}", text.strip()) if b.strip()]
    blocks.extend(r for r in rows if r)
    return MessageSpec(tuple(blocks), color)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(spec: MessageSpec) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=spec.color)
    for i, block in enumerate(spec.blocks):
        if i > 0:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        if isinstance(block, str):
            container.add_item(discord.ui.TextDisplay(_truncate(block, 3900)))
            continue
        row = discord.ui.ActionRow()
        for comp in block:
            if isinstance(comp, ButtonSpec):
                row.add_item(
                    discord.ui.Button(
                        label=_truncate(comp.label, 80),
                        custom_id=comp.custom_id,
                        style=_STYLES[comp.style],
                        disabled=comp.disabled,
                        emoji=comp.emoji,
                    )
                )
            else:
                row.add_item(
                    discord.ui.Select(
                        custom_id=comp.custom_id,
                        placeholder=_truncate(comp.placeholder, 150),
                        disabled=comp.disabled,
                        options=[
                            discord.SelectOption(
                                label=_truncate(o.label, 100),
                                value=o.value,
                                description=_truncate(o.description, 100) if o.description else None,
                            )
                            for o in comp.options[:25]
                        ],
                    )
                )
        container.add_item(row)
    view.add_item(container)
    view.stop()  # pas de stockage côté discord.py : le routeur du bot gère les clics
    return view


# --------------------------------------------------------------------------- réponses éphémères
async def respond(interaction: discord.Interaction, text: str, color: int = Colors.BRAND, *rows: Row) -> None:
    """Première réponse à une interaction : nouveau message éphémère."""
    await interaction.response.send_message(view=render(message(text, color, *rows)), ephemeral=True)


async def update(interaction: discord.Interaction, text: str, color: int = Colors.BRAND, *rows: Row) -> None:
    """Met à jour le message de l'interaction (le message cliqué, ou la réponse déjà envoyée)."""
    view = render(message(text, color, *rows))
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


# --------------------------------------------------------------------------- formats
def fmt_duration(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} j {hours} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def fmt_dt(value: datetime, tz: tzinfo) -> str:
    return value.astimezone(tz).strftime("%d/%m/%Y %H:%M")


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    gb = n / 1024**3
    if gb >= 1:
        return f"{gb:.1f} Go".replace(".", ",")
    return f"{n / 1024**2:.0f} Mo"


def progress_bar(percent: float, width: int = 12) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = round(percent / 100 * width)
    return f"`{'█' * filled}{'░' * (width - filled)}` {percent:.0f} %"
