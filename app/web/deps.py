"""FastAPI-Dependencies: Authentifizierung und serverseitige Berechtigungsprüfung für jede Guild-Route."""
from __future__ import annotations

from dataclasses import dataclass

import discord
from fastapi import Depends, HTTPException, Request

from app.config import settings
from app.db.models import DashboardSession
from app.runtime import get_bot
from app.services.notify import is_admin, is_staff
from app.web.security import check_csrf, load_session, rate_limit

LEVELS = {"staff": 1, "admin": 2}


@dataclass
class Ctx:
    session: DashboardSession
    guild: discord.Guild
    member: discord.Member | None
    level: str
    is_owner: bool

    @property
    def user_id(self) -> int:
        return self.session.user_id

    @property
    def actor(self) -> discord.abc.User:
        return self.member or discord.Object(self.session.user_id)  # type: ignore[return-value]

    @property
    def actor_name(self) -> str:
        return str(self.member) if self.member else self.session.username


async def require_session(request: Request) -> DashboardSession:
    s = await load_session(request)
    if s is None:
        raise HTTPException(401, "Nicht angemeldet")
    await check_csrf(request, s)
    if request.method not in ("GET", "HEAD"):
        await rate_limit(f"user:{s.user_id}:write", 90, 60)
    return s


async def require_owner(session: DashboardSession = Depends(require_session)) -> DashboardSession:
    if session.user_id not in settings.owner_id_set:
        raise HTTPException(403, "Nur für Bot-Owner")
    return session


async def resolve_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None
    return member


async def access_level(guild: discord.Guild, user_id: int) -> tuple[str | None, discord.Member | None]:
    member = await resolve_member(guild, user_id)
    if user_id in settings.owner_id_set:
        return "admin", member
    if member is None:
        return None, None
    if await is_admin(member):
        return "admin", member
    if await is_staff(member):
        return "staff", member
    return None, member


def guild_ctx(min_level: str = "staff"):
    async def dep(guild_id: int, session: DashboardSession = Depends(require_session)) -> Ctx:
        bot = get_bot()
        guild = bot.get_guild(guild_id)
        if guild is None:
            raise HTTPException(404, "Server nicht gefunden oder Bot nicht auf dem Server")
        level, member = await access_level(guild, session.user_id)
        if level is None or LEVELS[level] < LEVELS[min_level]:
            raise HTTPException(403, "Keine Berechtigung für diesen Bereich")
        return Ctx(session, guild, member, level, session.user_id in settings.owner_id_set)

    return dep


staff = guild_ctx("staff")
admin = guild_ctx("admin")
