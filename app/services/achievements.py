"""Achievements (fest definiert, Fortschritt aus Member-Statistiken) und Badges (konfigurierbar)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import discord
from sqlalchemy import select

from app.core.embeds import theme
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import i18n
from app.db.base import SessionLocal, session_scope
from app.db.models import Badge, Member, MemberAchievement, MemberBadge

log = logging.getLogger("nova.achievements")


@dataclass(frozen=True)
class Achievement:
    key: str
    emoji: str
    target: int
    value: Callable[[Member], int]


ACHIEVEMENTS: list[Achievement] = [
    Achievement("first_message", "💬", 1, lambda m: m.messages),
    Achievement("messages_100", "📨", 100, lambda m: m.messages),
    Achievement("messages_1000", "📬", 1000, lambda m: m.messages),
    Achievement("messages_10000", "🏛️", 10000, lambda m: m.messages),
    Achievement("level_10", "⭐", 10, lambda m: m.level),
    Achievement("level_25", "🌟", 25, lambda m: m.level),
    Achievement("level_50", "💫", 50, lambda m: m.level),
    Achievement("voice_10h", "🎙️", 600, lambda m: m.voice_minutes),
    Achievement("voice_100h", "🎧", 6000, lambda m: m.voice_minutes),
    Achievement("giveaway_winner", "🏆", 1, lambda m: m.giveaways_won),
    Achievement("event_winner", "🥇", 1, lambda m: m.events_won),
    Achievement("streak_7", "🔥", 7, lambda m: m.daily_best_streak),
    Achievement("streak_30", "☄️", 30, lambda m: m.daily_best_streak),
    Achievement("stream_fan", "🎥", 10, lambda m: m.stream_checkins),
    Achievement("rich", "💎", 100_000, lambda m: m.coins_earned),
    Achievement("booster", "💜", 1, lambda m: 0),  # extern gesetzt (Boost)
    Achievement("og_member", "⭐", 1, lambda m: 0),  # extern gesetzt (Beitrittsdatum)
]
ACH_MAP = {a.key: a for a in ACHIEVEMENTS}


def progress_for(member: Member, unlocked: set[str], extra: dict[str, bool] | None = None) -> list[dict]:
    out = []
    for a in ACHIEVEMENTS:
        cur = a.value(member)
        if extra and extra.get(a.key):
            cur = a.target
        done = a.key in unlocked
        out.append({"key": a.key, "emoji": a.emoji, "target": a.target, "value": min(cur, a.target) if not done else a.target, "unlocked": done})
    return out


def _og_cutoff(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


async def check(bot, guild: discord.Guild, member_row: Member, discord_member: discord.Member | None = None) -> list[str]:
    """Prüft alle Achievements, speichert neue und kündigt sie an. Gibt neue Keys zurück."""
    if not await config.enabled(guild.id, "profiles"):
        return []
    cfg = await config.get(guild.id, "profiles")
    async with SessionLocal() as s:
        have = set((await s.execute(select(MemberAchievement.key).where(
            MemberAchievement.guild_id == guild.id, MemberAchievement.user_id == member_row.user_id))).scalars())
    new: list[str] = []
    for a in ACHIEVEMENTS:
        if a.key in have:
            continue
        reached = a.value(member_row) >= a.target
        if a.key == "booster":
            reached = bool(discord_member and discord_member.premium_since)
        if a.key == "og_member":
            cutoff = _og_cutoff(cfg.get("og_before"))
            first = member_row.first_joined_at
            reached = bool(cutoff and first and first.replace(tzinfo=first.tzinfo or timezone.utc) < cutoff)
        if reached:
            new.append(a.key)
    if not new:
        return []
    async with session_scope() as s:
        for key in new:
            s.add(MemberAchievement(guild_id=guild.id, user_id=member_row.user_id, key=key))
    bus.publish(guild.id, "achievement", {"user_id": str(member_row.user_id), "keys": new})

    channel = guild.get_channel(cfg.id("achievement_channel") or 0)
    if isinstance(channel, discord.TextChannel):
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        lines = "\n".join(f"{ACH_MAP[k].emoji} **{_(f'ach.{k}.name')}** — {_(f'ach.{k}.desc')}" for k in new)
        user = discord_member or guild.get_member(member_row.user_id)
        e = th.embed(_("ach.unlocked_title"), f"{user.mention if user else ''}\n{lines}", kind="primary", user=user)
        try:
            await channel.send(embed=e, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass
    return new


async def badges_for(guild: discord.Guild, member_row: Member, member: discord.Member | None) -> list[Badge]:
    """Automatische + manuell vergebene Badges."""
    async with SessionLocal() as s:
        badges = (await s.execute(select(Badge).where(Badge.guild_id == guild.id).order_by(Badge.position, Badge.id))).scalars().all()
        manual = set((await s.execute(select(MemberBadge.badge_id).where(
            MemberBadge.guild_id == guild.id, MemberBadge.user_id == member_row.user_id))).scalars())
    gen = await config.get(guild.id, "general")
    staff_roles = set(gen.ids("staff_roles")) | set(gen.ids("admin_roles"))
    role_ids = {r.id for r in member.roles} if member else set()
    out = []
    for b in badges:
        rule, val = b.auto_rule, (b.rule_value or "")
        ok = b.id in manual
        if rule == "owner":
            ok = ok or member_row.user_id == guild.owner_id
        elif rule == "staff":
            ok = ok or bool(role_ids & staff_roles) or bool(member and member.guild_permissions.manage_guild)
        elif rule == "booster":
            ok = ok or bool(member and member.premium_since)
        elif rule == "giveaway_winner":
            ok = ok or member_row.giveaways_won > 0
        elif rule == "og":
            cutoff = _og_cutoff(val)
            first = member_row.first_joined_at
            ok = ok or bool(cutoff and first and first.replace(tzinfo=first.tzinfo or timezone.utc) < cutoff)
        elif rule == "role":
            ok = ok or (val.isdigit() and int(val) in role_ids)
        elif rule == "level":
            ok = ok or (val.isdigit() and member_row.level >= int(val))
        if ok:
            out.append(b)
    return out
