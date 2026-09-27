"""Guild-Datensätze und fortlaufende Nummern (Cases, Tickets, Suggestions)."""
from __future__ import annotations

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import session_scope, utcnow
from app.db.models import Guild


async def ensure_guild(session: AsyncSession, guild: discord.Guild | int) -> Guild:
    gid = guild if isinstance(guild, int) else guild.id
    row = await session.get(Guild, gid)
    if row is None:
        row = Guild(id=gid, next_case=1, next_ticket=1, next_suggestion=1, active=True)
        session.add(row)
    if isinstance(guild, discord.Guild):
        row.name = guild.name[:100]
        row.icon = guild.icon.url[:255] if guild.icon else None
        row.owner_id = guild.owner_id
        row.active = True
        row.left_at = None
    return row


async def next_number(session: AsyncSession, guild_id: int, field: str) -> int:
    """Atomar hochzählen (SELECT … FOR UPDATE in PostgreSQL)."""
    row = (await session.execute(select(Guild).where(Guild.id == guild_id).with_for_update())).scalar_one_or_none()
    if row is None:
        row = await ensure_guild(session, guild_id)
        await session.flush()
    value = getattr(row, field) or 1
    setattr(row, field, value + 1)
    return value


async def mark_left(guild_id: int) -> None:
    async with session_scope() as s:
        row = await s.get(Guild, guild_id)
        if row:
            row.active = False
            row.left_at = utcnow()
