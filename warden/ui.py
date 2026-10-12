"""Components V2 layout (container + colour bar + separators), like the old bot.

Messages are first described as "specs" (pure, comparable data), then rendered as
`discord.ui.LayoutView`. Views are sent "stopped": discord.py does not keep them in memory
and every click goes through the bot's `on_interaction` router, which keeps buttons working
across bot restarts.
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


Row = tuple  # tuple[ButtonSpec, ...] or tuple[SelectSpec]


@dataclass(frozen=True)
class MessageSpec:
    """Blocks shown in order, separated by a separator: text (str) or a row of components."""

    blocks: tuple = field(default_factory=tuple)
    color: int = Colors.BRAND


def message(text: str, color: int = Colors.BRAND, *rows: Row) -> MessageSpec:
    """Text split into paragraphs (empty line = separator), then rows of components."""
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
    view.stop()  # no storage on the discord.py side: the bot's router handles clicks
    return view


# --------------------------------------------------------------------------- ephemeral replies
async def respond(interaction: discord.Interaction, text: str, color: int = Colors.BRAND, *rows: Row) -> None:
    """First reply to an interaction: new ephemeral message."""
    await interaction.response.send_message(view=render(message(text, color, *rows)), ephemeral=True)


async def update(interaction: discord.Interaction, text: str, color: int = Colors.BRAND, *rows: Row) -> None:
    """Update the interaction's message (the clicked message, or the reply already sent)."""
    view = render(message(text, color, *rows))
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


# --------------------------------------------------------------------------- untrusted text
# Player names (A2S), save names (chosen in game), Discord nicknames, values read from
# ServerSettings.ini: never interpreted as markdown (fake links, headings, formatting).
def md(text: object) -> str:
    """Text shown as is in a markdown message."""
    return discord.utils.escape_markdown(str(text))


def code(text: object) -> str:
    """Text placed between backticks: a ` in the value would close the block."""
    return str(text).replace("`", "ˋ")


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
