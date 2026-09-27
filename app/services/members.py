"""Member-Profile laden/anlegen."""
from __future__ import annotations

import discord
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.guild_config import config
from app.db.base import utcnow
from app.db.models import Member


async def ensure_member(session: AsyncSession, guild_id: int, user: discord.abc.User | int) -> Member:
    user_id = user if isinstance(user, int) else user.id
    row = await session.get(Member, (guild_id, user_id))
    if row is None:
        eco = await config.get(guild_id, "economy")
        row = Member(guild_id=guild_id, user_id=user_id, coins=int(eco.get("starting_balance") or 0), xp=0, level=0,
                     messages=0, voice_minutes=0, reactions=0, stream_checkins=0, events_won=0, giveaways_won=0,
                     invites=0, coins_earned=0, daily_streak=0, daily_best_streak=0, weekly_streak=0, games={})
        session.add(row)
    if not isinstance(user, int):
        row.username = str(user)[:100]
        row.display_name = getattr(user, "display_name", str(user))[:100]
        row.avatar = user.display_avatar.url[:255]
        joined = getattr(user, "joined_at", None)
        if joined:
            row.joined_at = joined
            if row.first_joined_at is None:
                row.first_joined_at = joined
        if isinstance(user, discord.Member):
            row.in_guild = True
    return row


def touch_leave(row: Member) -> None:
    row.in_guild = False
    row.left_at = utcnow()
