"""Gemeinsame Helfer für API-Routen."""
from __future__ import annotations

import time
from typing import Any

import discord
import psutil
from sqlalchemy import select

from app.core.schema import ValidationContext
from app.db.base import SessionLocal
from app.db.models import Badge
from app.runtime import STARTED_AT

_proc = psutil.Process()
_proc.cpu_percent(None)


def sid(v: int | None) -> str | None:
    return str(v) if v is not None else None


def user_json(u: discord.abc.User | None, fallback_id: int | None = None) -> dict[str, Any] | None:
    if u is None:
        return {"id": sid(fallback_id), "name": str(fallback_id), "avatar": None} if fallback_id else None
    return {"id": str(u.id), "name": str(u), "display": getattr(u, "display_name", u.name), "avatar": u.display_avatar.with_size(128).url,
            "bot": u.bot}


def bot_status(bot) -> dict[str, Any]:
    up = int(time.time() - STARTED_AT)
    mem = _proc.memory_info().rss
    return {
        "online": bot.is_ready() and not bot.is_closed(), "maintenance": bot.maintenance,
        "uptime": up, "latency": round(bot.latency * 1000) if bot.latency == bot.latency else None,
        "guilds": len(bot.guilds), "users": sum(g.member_count or 0 for g in bot.guilds),
        "memory_mb": round(mem / 1024 / 1024, 1), "cpu": round(_proc.cpu_percent(None), 1),
        "shards": bot.shard_count or 1, "system_cpu": psutil.cpu_percent(None), "system_mem": psutil.virtual_memory().percent,
        "commands": len(bot.tree.get_commands()), "modules": len(getattr(bot, "module_names", [])),
    }


def channel_json(c: discord.abc.GuildChannel) -> dict[str, Any]:
    return {"id": str(c.id), "name": c.name, "type": str(c.type), "category": c.category.name if getattr(c, "category", None) else None,
            "position": c.position}


def role_json(r: discord.Role, me: discord.Member | None = None) -> dict[str, Any]:
    return {"id": str(r.id), "name": r.name, "color": str(r.color) if r.color.value else None, "position": r.position,
            "managed": r.managed, "hoist": r.hoist, "mentionable": r.mentionable, "members": len(r.members),
            "permissions": str(r.permissions.value), "default": r.is_default(),
            "editable": bool(me and r < me.top_role and not r.managed and not r.is_default()), "icon": r.display_icon.url if isinstance(r.display_icon, discord.Asset) else r.display_icon}


async def validation_ctx(guild: discord.Guild) -> ValidationContext:
    async with SessionLocal() as db:
        badges = [str(b) for b in (await db.execute(select(Badge.id).where(Badge.guild_id == guild.id))).scalars()]
    return ValidationContext({str(c.id): str(c.type) for c in guild.channels}, [str(r.id) for r in guild.roles], badges)


def paginate(page: int, per_page: int) -> tuple[int, int]:
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    return (page - 1) * per_page, per_page
