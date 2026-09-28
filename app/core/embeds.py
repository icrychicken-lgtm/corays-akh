"""Einheitliches, konfigurierbares Embed-Design (Success / Error / Warning / Info / Primary).

Clean-Stil: Farbe + Titel + Text. Kein Icon vor dem Titel, keine Uhrzeit, keine Fußzeile – außer ein Modul
setzt sie bewusst (z. B. „Seite 1 von 2“)."""
from __future__ import annotations

from dataclasses import dataclass

import discord

from app.core.guild_config import config
from app.db.base import utcnow

ICONS = {"success": "✅", "error": "⛔", "warning": "⚠️", "info": "ℹ️", "primary": ""}
DEFAULTS = {"primary": "#c8a45d", "success": "#5fb37a", "error": "#d25a5a", "warning": "#d9a441", "info": "#7a8ba8"}


def hex_to_color(value: str | None, fallback: str = "#8b5cf6") -> discord.Colour:
    try:
        return discord.Colour(int((value or fallback).lstrip("#"), 16))
    except ValueError:
        return discord.Colour(int(fallback.lstrip("#"), 16))


@dataclass
class Theme:
    colors: dict[str, str]
    footer: str
    icon_url: str | None

    def color(self, kind: str) -> discord.Colour:
        return hex_to_color(self.colors.get(kind), DEFAULTS.get(kind, DEFAULTS["primary"]))

    def embed(self, title: str | None = None, description: str | None = None, *, kind: str = "primary",
              user: discord.abc.User | None = None, timestamp: bool = False, icon: bool = False, footer: bool = False) -> discord.Embed:
        """`icon`, `timestamp`, `footer` sind im Clean-Stil aus und nur noch auf ausdrücklichen Wunsch an."""
        prefix = ICONS.get(kind, "") if icon else ""
        e = discord.Embed(
            title=f"{prefix}  {title}".strip() if title else None,
            description=description,
            colour=self.color(kind),
            timestamp=utcnow() if timestamp else None,
        )
        if user is not None:
            e.set_author(name=getattr(user, "display_name", str(user)), icon_url=user.display_avatar.url)
        if footer:
            e.set_footer(text=self.footer, icon_url=self.icon_url)
        return e

    def success(self, title: str, description: str | None = None, **kw) -> discord.Embed:
        return self.embed(title, description, kind="success", **kw)

    def error(self, title: str, description: str | None = None, **kw) -> discord.Embed:
        return self.embed(title, description, kind="error", **kw)

    def warning(self, title: str, description: str | None = None, **kw) -> discord.Embed:
        return self.embed(title, description, kind="warning", **kw)

    def info(self, title: str, description: str | None = None, **kw) -> discord.Embed:
        return self.embed(title, description, kind="info", **kw)


async def theme(guild: discord.Guild | None) -> Theme:
    if guild is None:
        return Theme(dict(DEFAULTS), "Nova", None)
    cfg = await config.get(guild.id, "general")
    colors = {k: cfg.get(f"color_{k}") or v for k, v in DEFAULTS.items()}
    icon = guild.icon.url if (guild.icon and cfg.get("use_server_icon", True)) else None
    return Theme(colors, cfg.get("footer_text") or guild.name, icon)


def progress_bar(value: float, total: float, length: int = 14) -> str:
    ratio = 0 if total <= 0 else max(0.0, min(1.0, value / total))
    filled = round(ratio * length)
    return "▰" * filled + "▱" * (length - filled)


def fmt_num(n: int | float) -> str:
    return f"{int(n):,}".replace(",", ".")


def divider() -> str:
    return "━━━━━━━━━━━━━━━━━━━━"
