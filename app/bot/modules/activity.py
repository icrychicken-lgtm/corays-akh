"""Aktivitäts-Tracking: Nachrichten, Voice-Minuten (Anti-AFK), Reaktionen, Games.
Vergibt XP & Coins mit Anti-Spam-Cooldowns und füttert Quests, Achievements und Analytics.
Zähler werden gepuffert und minütlich gebündelt geschrieben (keine DB-Last pro Nachricht)."""
from __future__ import annotations

import logging
import random
import time
from collections import defaultdict

import discord
from discord.ext import commands, tasks

from app.config import settings
from app.core.guild_config import config
from app.core.metrics import metrics
from app.db.base import session_scope
from app.services import achievements, progression
from app.services.members import ensure_member

log = logging.getLogger("nova.activity")


class Activity(commands.Cog):
    module = "core"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.buffer: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.games: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.xp_cd: dict[tuple[int, int], float] = {}
        self.coin_cd: dict[tuple[int, int], float] = {}
        self.react_cd: dict[tuple[int, int], float] = {}
        self.flush_loop.start()
        self.voice_loop.start()

    def cog_unload(self):
        self.flush_loop.cancel()
        self.voice_loop.cancel()

    @staticmethod
    def _excluded(member: discord.Member, channel_id: int | None, cfg) -> bool:
        if channel_id and channel_id in cfg.ids("no_xp_channels"):
            return True
        return bool({r.id for r in member.roles} & set(cfg.ids("no_xp_roles")))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return
        gid, uid = message.guild.id, message.author.id
        metrics.incr(gid, "messages")
        self.buffer[(gid, uid)]["messages"] += 1
        now = time.monotonic()

        if await config.enabled(gid, "levels"):
            cfg = await config.get(gid, "levels")
            parent = getattr(message.channel, "parent_id", None)
            if not self._excluded(message.author, message.channel.id, cfg) and not (parent and parent in cfg.ids("no_xp_channels")):
                if self.xp_cd.get((gid, uid), 0) <= now:
                    self.xp_cd[(gid, uid)] = now + cfg.get("cooldown", 60)
                    lo, hi = sorted((cfg.get("xp_min", 15), cfg.get("xp_max", 25)))
                    await progression.add_xp(self.bot, message.guild, message.author, random.randint(lo, hi), channel=message.channel)

        if await config.enabled(gid, "economy"):
            eco = await config.get(gid, "economy")
            if self.coin_cd.get((gid, uid), 0) <= now:
                self.coin_cd[(gid, uid)] = now + eco.get("message_cooldown", 60)
                lo, hi = sorted((eco.get("message_min", 1), eco.get("message_max", 5)))
                amount = random.randint(lo, hi)
                if amount:
                    await progression.add_coins(gid, message.author, amount, reason="message")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return
        gid, uid = payload.guild_id, payload.user_id
        self.buffer[(gid, uid)]["reactions"] += 1
        metrics.incr(gid, "reactions")
        now = time.monotonic()
        if self.react_cd.get((gid, uid), 0) > now or not await config.enabled(gid, "levels"):
            return
        self.react_cd[(gid, uid)] = now + 15
        cfg = await config.get(gid, "levels")
        if cfg.get("reaction_xp") and not self._excluded(payload.member, payload.channel_id, cfg):
            await progression.add_xp(self.bot, payload.member.guild, payload.member, int(cfg["reaction_xp"]), announce=False)

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        if after.bot:
            return
        new_games = {a.name for a in after.activities if a.type == discord.ActivityType.playing and a.name}
        old_games = {a.name for a in before.activities if a.type == discord.ActivityType.playing and a.name}
        started = new_games - old_games
        if started and await config.enabled(after.guild.id, "profiles"):
            cfg = await config.get(after.guild.id, "profiles")
            if cfg.get("track_games", True):
                for g in started:
                    self.games[(after.guild.id, after.id)][g[:60]] += 1

    # ── Voice: jede Minute ──
    @tasks.loop(seconds=60)
    async def voice_loop(self):
        for guild in self.bot.guilds:
            levels_on = await config.enabled(guild.id, "levels")
            eco_on = await config.enabled(guild.id, "economy")
            lcfg = await config.get(guild.id, "levels")
            ecfg = await config.get(guild.id, "economy")
            total = 0
            for vc in guild.voice_channels + list(guild.stage_channels):
                if guild.afk_channel and vc.id == guild.afk_channel.id:
                    continue
                humans = [m for m in vc.members if not m.bot]
                active = [m for m in humans if not (m.voice and (m.voice.self_deaf or m.voice.deaf or m.voice.afk))]
                if lcfg.get("voice_needs_others", True) and len(active) < 2:
                    continue
                for m in active:
                    total += 1
                    self.buffer[(guild.id, m.id)]["voice_minutes"] += 1
                    if levels_on and lcfg.get("voice_xp") and not self._excluded(m, vc.id, lcfg):
                        await progression.add_xp(self.bot, guild, m, int(lcfg["voice_xp"]), announce=True)
                    if eco_on and ecfg.get("voice_per_minute"):
                        await progression.add_coins(guild.id, m, int(ecfg["voice_per_minute"]), reason="voice")
            if total:
                metrics.incr(guild.id, "voice_minutes", total)

    @voice_loop.before_loop
    async def _vbefore(self):
        await self.bot.wait_until_ready()

    # ── Puffer schreiben ──
    @tasks.loop(seconds=60)
    async def flush_loop(self):
        await self.flush()

    @flush_loop.before_loop
    async def _fbefore(self):
        await self.bot.wait_until_ready()

    async def flush(self) -> None:
        if not self.buffer and not self.games:
            return
        buf, games = self.buffer, self.games
        self.buffer, self.games = defaultdict(lambda: defaultdict(int)), defaultdict(lambda: defaultdict(int))
        rows = {}
        try:
            async with session_scope() as s:
                for (gid, uid) in set(buf) | set(games):
                    guild = self.bot.get_guild(gid)
                    if guild is None:
                        continue
                    member = guild.get_member(uid)
                    row = await ensure_member(s, gid, member or uid)
                    c = buf.get((gid, uid), {})
                    row.messages += c.get("messages", 0)
                    row.voice_minutes += c.get("voice_minutes", 0)
                    row.reactions += c.get("reactions", 0)
                    if (gid, uid) in games:
                        g = dict(row.games or {})
                        for name, n in games[(gid, uid)].items():
                            g[name] = g.get(name, 0) + n
                        row.games = dict(sorted(g.items(), key=lambda kv: -kv[1])[:25])
                    rows[(gid, uid)] = row
        except Exception:
            log.exception("Aktivitäts-Flush fehlgeschlagen")
            return
        for (gid, uid), row in rows.items():
            guild = self.bot.get_guild(gid)
            c = buf.get((gid, uid), {})
            try:
                for metric in ("messages", "voice_minutes", "reactions"):
                    if c.get(metric):
                        await progression.quest_progress(self.bot, guild, uid, metric, c[metric])
                if c:
                    await achievements.check(self.bot, guild, row, guild.get_member(uid))
            except Exception:
                log.exception("Quest/Achievement-Update fehlgeschlagen")


async def setup(bot: commands.Bot):
    await bot.add_cog(Activity(bot))
