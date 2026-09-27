"""XP, Level, Coins, Level-Belohnungen und Quest-Fortschritt."""
from __future__ import annotations

import logging
import time
from typing import Any

import discord
from sqlalchemy import select

from app.core.embeds import theme
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import fill, i18n
from app.core.metrics import metrics
from app.core.timeutil import day_key, week_key
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import LevelReward, Member, MemberBadge, Quest, QuestProgress
from app.services import achievements
from app.services.members import ensure_member

log = logging.getLogger("nova.progression")


# ───────────────────────── Level-Formel ─────────────────────────
def xp_to_next(level: int) -> int:
    return 5 * level * level + 50 * level + 100


def total_for_level(level: int) -> int:
    return sum(xp_to_next(i) for i in range(level))


def level_from_xp(xp: int) -> tuple[int, int, int]:
    """→ (level, xp in aktuellem Level, xp bis zum nächsten Level)."""
    level = 0
    while xp >= xp_to_next(level):
        xp -= xp_to_next(level)
        level += 1
    return level, xp, xp_to_next(level)


async def multiplier_for(member: discord.Member) -> float:
    cfg = await config.get(member.guild.id, "levels")
    role_ids = {r.id for r in member.roles}
    best = 1.0
    for row in cfg.get("multipliers") or []:
        rid = row.get("role")
        if rid and int(rid) in role_ids:
            best = max(best, float(row.get("multiplier") or 1))
    return best


# ───────────────────────── XP ─────────────────────────
async def add_xp(bot, guild: discord.Guild, user: discord.Member | int, amount: int, *,
                 channel: discord.abc.Messageable | None = None, use_multiplier: bool = True, announce: bool = True) -> Member | None:
    if amount == 0 or not await config.enabled(guild.id, "levels"):
        return None
    member = user if isinstance(user, discord.Member) else guild.get_member(user)
    if use_multiplier and member is not None and amount > 0:
        amount = int(round(amount * await multiplier_for(member)))
    async with session_scope() as s:
        row = await ensure_member(s, guild.id, member or user)
        old_level = row.level
        row.xp = max(0, row.xp + amount)
        row.level = level_from_xp(row.xp)[0]
        row.last_xp_at = utcnow()
        new_level = row.level
    metrics.incr(guild.id, "xp", max(amount, 0))
    if new_level != old_level:
        bus.publish(guild.id, "level", {"user_id": str(row.user_id), "level": new_level})
        if member is not None:
            await apply_level_rewards(guild, member, new_level)
            if new_level > old_level and announce:
                await _announce_level(bot, guild, member, new_level, channel)
            await achievements.check(bot, guild, row, member)
    return row


async def set_xp(bot, guild: discord.Guild, user_id: int, xp: int) -> Member:
    async with session_scope() as s:
        row = await ensure_member(s, guild.id, guild.get_member(user_id) or user_id)
        row.xp = max(0, xp)
        row.level = level_from_xp(row.xp)[0]
    member = guild.get_member(user_id)
    if member:
        await apply_level_rewards(guild, member, row.level)
    return row


async def apply_level_rewards(guild: discord.Guild, member: discord.Member, level: int) -> None:
    cfg = await config.get(guild.id, "levels")
    async with SessionLocal() as s:
        rewards = (await s.execute(select(LevelReward).where(LevelReward.guild_id == guild.id).order_by(LevelReward.level))).scalars().all()
    if not rewards:
        return
    earned = [r for r in rewards if r.level <= level]
    unearned = [r for r in rewards if r.level > level]
    keep = {r.role_id for r in earned} if cfg.get("stack_rewards", True) else ({earned[-1].role_id} if earned else set())
    me = guild.me
    add = [guild.get_role(rid) for rid in keep]
    remove = [guild.get_role(r.role_id) for r in earned + unearned if r.role_id not in keep]
    add = [r for r in add if r and r not in member.roles and r < me.top_role]
    remove = [r for r in remove if r and r in member.roles and r < me.top_role]
    try:
        if add:
            await member.add_roles(*add, reason=f"Level {level} Belohnung")
        if remove:
            await member.remove_roles(*remove, reason=f"Level {level} Belohnung")
    except discord.HTTPException as exc:
        log.warning("Level-Rollen konnten nicht gesetzt werden (%s): %s", guild.id, exc)


async def _announce_level(bot, guild: discord.Guild, member: discord.Member, level: int, channel) -> None:
    cfg = await config.get(guild.id, "levels")
    mode = cfg.get("announce", "channel")
    if mode == "off":
        return
    th = await theme(guild)
    _ = await i18n.for_guild(guild.id)
    text = fill(cfg.get("announce_message") or _("levels.levelup_default"), user=member.mention, username=member.display_name,
                level=level, server=guild.name)
    e = th.embed(_("levels.levelup_title"), text, user=member)
    e.set_thumbnail(url=member.display_avatar.url)
    try:
        if mode == "dm":
            await member.send(embed=e)
            return
        target = guild.get_channel(cfg.id("announce_channel") or 0) if mode == "custom" else channel
        if target is not None:
            await target.send(content=member.mention, embed=e, allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass


# ───────────────────────── Coins ─────────────────────────
async def add_coins(guild_id: int, user: discord.abc.User | int, amount: int, *, reason: str = "") -> int:
    async with session_scope() as s:
        row = await ensure_member(s, guild_id, user)
        row.coins = max(0, row.coins + amount)
        if amount > 0:
            row.coins_earned += amount
        balance = row.coins
    if amount > 0:
        metrics.incr(guild_id, "coins", amount)
    bus.publish(guild_id, "coins", {"user_id": str(row.user_id), "balance": balance, "delta": amount, "reason": reason})
    return balance


# ───────────────────────── Quests ─────────────────────────
QUEST_METRICS = {
    "messages": "Nachrichten schreiben", "voice_minutes": "Minuten im Voice", "reactions": "Reaktionen geben",
    "stream_checkins": "Stream-Check-ins", "events_won": "Events gewinnen", "daily_claims": "Daily abholen",
    "giveaways_joined": "Giveaways beitreten",
}
_quest_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}


def invalidate_quests(guild_id: int) -> None:
    _quest_cache.pop(guild_id, None)


async def _quests(guild_id: int) -> list[dict[str, Any]]:
    hit = _quest_cache.get(guild_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    async with SessionLocal() as s:
        rows = (await s.execute(select(Quest).where(Quest.guild_id == guild_id, Quest.enabled.is_(True)))).scalars().all()
    data = [r.to_dict() | {"id": r.id, "reward_role_id": r.reward_role_id} for r in rows]
    _quest_cache[guild_id] = (time.monotonic() + 60, data)
    return data


async def quest_progress(bot, guild: discord.Guild, user_id: int, metric: str, amount: int = 1) -> None:
    if amount <= 0 or not await config.enabled(guild.id, "quests"):
        return
    quests = [q for q in await _quests(guild.id) if q["metric"] == metric]
    if not quests:
        return
    gen = await config.get(guild.id, "general")
    tzname = gen.get("timezone")
    completed: list[dict[str, Any]] = []
    async with session_scope() as s:
        for q in quests:
            period = day_key(tzname) if q["period"] == "daily" else week_key(tzname)
            row = await s.get(QuestProgress, (q["id"], user_id, period))
            if row is None:
                row = QuestProgress(quest_id=q["id"], user_id=user_id, period_key=period, guild_id=guild.id, progress=0)
                s.add(row)
            if row.completed_at:
                continue
            row.progress = min(q["target"], row.progress + amount)
            if row.progress >= q["target"]:
                row.completed_at = utcnow()
                completed.append(q)
    for q in completed:
        await _reward_quest(bot, guild, user_id, q)


async def _reward_quest(bot, guild: discord.Guild, user_id: int, q: dict[str, Any]) -> None:
    member = guild.get_member(user_id)
    if q.get("reward_coins"):
        await add_coins(guild.id, member or user_id, int(q["reward_coins"]), reason="quest")
    if q.get("reward_xp"):
        await add_xp(bot, guild, member or user_id, int(q["reward_xp"]), use_multiplier=False, announce=True)
    if q.get("reward_role_id") and member:
        role = guild.get_role(int(q["reward_role_id"]))
        if role and role < guild.me.top_role:
            try:
                await member.add_roles(role, reason="Quest-Belohnung")
            except discord.HTTPException:
                pass
    if q.get("reward_badge_id"):
        async with session_scope() as s:
            if not await s.get(MemberBadge, (guild.id, user_id, int(q["reward_badge_id"]))):
                s.add(MemberBadge(guild_id=guild.id, user_id=user_id, badge_id=int(q["reward_badge_id"])))
    bus.publish(guild.id, "quest", {"user_id": str(user_id), "quest": q["name"]})

    cfg = await config.get(guild.id, "quests")
    eco = await config.get(guild.id, "economy")
    _ = await i18n.for_guild(guild.id)
    th = await theme(guild)
    rewards = []
    if q.get("reward_xp"):
        rewards.append(f"✨ {q['reward_xp']} XP")
    if q.get("reward_coins"):
        rewards.append(f"{eco.get('currency_emoji')} {q['reward_coins']} {eco.get('currency_name')}")
    if q.get("reward_role_id"):
        rewards.append(f"🎭 <@&{q['reward_role_id']}>")
    e = th.success(_("quests.completed_title"), _("quests.completed_desc", quest=q["name"], rewards=" · ".join(rewards) or "—"))
    channel = guild.get_channel(cfg.id("announce_channel") or 0)
    try:
        if channel is not None and member is not None:
            await channel.send(content=member.mention, embed=e, allowed_mentions=discord.AllowedMentions(users=True))
        if cfg.get("dm_on_complete") and member is not None:
            await member.send(embed=e)
    except discord.HTTPException:
        pass
