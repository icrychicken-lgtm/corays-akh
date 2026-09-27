"""Benachrichtigungs-Router (Modul 'notifications') und Zugriffs-Helfer (Staff/Admin)."""
from __future__ import annotations

import logging
from typing import Any

import discord

from app.core.embeds import theme
from app.core.guild_config import config
from app.core.i18n import fill

log = logging.getLogger("nova.notify")


async def notify(guild: discord.Guild, event: str, *, embed: discord.Embed | None = None, view: discord.ui.View | None = None,
                 channel_override: int | None = None, **placeholders: Any) -> discord.Message | None:
    """Sendet eine Benachrichtigung gemäß Dashboard-Konfiguration. Gibt None zurück, wenn deaktiviert."""
    if not await config.enabled(guild.id, "notifications"):
        return None
    cfg = await config.get(guild.id, "notifications")
    if not cfg.get(f"{event}_enabled"):
        return None
    channel = guild.get_channel(channel_override or cfg.id(f"{event}_channel") or 0)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return None
    role_id = cfg.id(f"{event}_role")
    raw = cfg.get(f"{event}_message") or ""
    role_mention = f"<@&{role_id}>" if role_id else ""
    text = fill(raw, role=role_mention, **placeholders)
    if role_id and "{role}" not in raw:
        text = f"{role_mention} {text}"
    try:
        return await channel.send(
            content=text[:2000] or None, embed=embed, view=view or discord.utils.MISSING,
            allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
        )
    except discord.HTTPException as exc:
        log.warning("Benachrichtigung %s in %s fehlgeschlagen: %s", event, guild.id, exc)
        return None


async def is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild or member.id == member.guild.owner_id:
        return True
    cfg = await config.get(member.guild.id, "general")
    return bool({r.id for r in member.roles} & set(cfg.ids("admin_roles")))


async def is_staff(member: discord.Member, extra_roles: list[int] | None = None) -> bool:
    if await is_admin(member):
        return True
    cfg = await config.get(member.guild.id, "general")
    roles = set(cfg.ids("staff_roles")) | set(extra_roles or [])
    if member.guild_permissions.moderate_members or member.guild_permissions.manage_messages:
        return True
    return bool({r.id for r in member.roles} & roles)


async def send_log(guild: discord.Guild, kind: str, embed: discord.Embed) -> None:
    """kind: message | member | server | voice | default"""
    if not await config.enabled(guild.id, "logging"):
        return
    cfg = await config.get(guild.id, "logging")
    channel = guild.get_channel(cfg.id(f"{kind}_channel") or cfg.id("default_channel") or 0)
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


async def simple_embed(guild: discord.Guild, title: str, description: str, kind: str = "primary") -> discord.Embed:
    th = await theme(guild)
    return th.embed(title, description, kind=kind)
