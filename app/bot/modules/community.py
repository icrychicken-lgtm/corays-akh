"""Community-Features: Starboard, AFK, Geburtstage."""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import and_, select

from app.bot.ui import reply
from app.core.embeds import theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import fill, i18n
from app.core.timeutil import local_now, since, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Member, StarboardEntry
from app.services import progression
from app.services.members import ensure_member

log = logging.getLogger("nova.community")


# ───────────────────────── Starboard ─────────────────────────
class Starboard(commands.Cog):
    module = "starboard"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _count(self, message: discord.Message, emoji: str, cfg) -> int:
        allowed = set(cfg.ids("allowed_roles"))
        for r in message.reactions:
            if str(r.emoji) != emoji:
                continue
            count = 0
            async for u in r.users(limit=500):
                if u.bot or (u.id == message.author.id and not cfg.get("self_star")):
                    continue
                if allowed:
                    m = message.guild.get_member(u.id)
                    if not m or not ({x.id for x in m.roles} & allowed):
                        continue
                count += 1
            return count
        return 0

    async def _handle(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or not await config.enabled(payload.guild_id, "starboard"):
            return
        cfg = await config.get(payload.guild_id, "starboard")
        emoji = cfg.get("emoji") or "⭐"
        if str(payload.emoji) != emoji:
            return
        guild = self.bot.get_guild(payload.guild_id)
        board = guild.get_channel(cfg.id("channel") or 0) if guild else None
        if not isinstance(board, discord.TextChannel) or payload.channel_id == board.id or payload.channel_id in cfg.ids("ignored_channels"):
            return
        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel is None or getattr(channel, "is_nsfw", lambda: False)():
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        stars = await self._count(message, emoji, cfg)
        async with session_scope() as db:
            entry = await db.get(StarboardEntry, message.id)
            if entry is None:
                if stars < cfg.get("threshold", 5):
                    return
                entry = StarboardEntry(message_id=message.id, guild_id=guild.id, channel_id=channel.id, author_id=message.author.id, stars=stars)
                db.add(entry)
            entry.stars = stars
            star_msg_id = entry.star_message_id
        th = await theme(guild)
        e = th.embed(None, message.content[:3900] or None, icon=False, user=message.author)
        e.colour = discord.Colour.gold()
        img = next((a.url for a in message.attachments if a.content_type and a.content_type.startswith("image/")), None)
        if img:
            e.set_image(url=img)
        e.add_field(name="​", value=f"[→ Original]({message.jump_url})")
        content = f"{emoji} **{stars}** · {channel.mention}"
        try:
            if star_msg_id:
                if stars < cfg.get("threshold", 5):
                    await board.get_partial_message(star_msg_id).delete()
                    async with session_scope() as db:
                        (await db.get(StarboardEntry, message.id)).star_message_id = None
                else:
                    await board.get_partial_message(star_msg_id).edit(content=content, embed=e)
            elif stars >= cfg.get("threshold", 5):
                sm = await board.send(content=content, embed=e)
                async with session_scope() as db:
                    (await db.get(StarboardEntry, message.id)).star_message_id = sm.id
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        await self._handle(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self._handle(payload)


# ───────────────────────── AFK ─────────────────────────
class AFK(commands.Cog):
    module = "afk"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.afk: dict[tuple[int, int], tuple[str, object]] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        async with SessionLocal() as db:
            rows = (await db.execute(select(Member).where(Member.afk_since.is_not(None)))).scalars().all()
        self.afk = {(r.guild_id, r.user_id): (r.afk_message or "", r.afk_since) for r in rows}

    @app_commands.command(name="afk", description="Setzt deinen AFK-Status")
    @app_commands.guild_only()
    async def afk_cmd(self, interaction: discord.Interaction, reason: app_commands.Range[str, 0, 200] = ""):
        member: discord.Member = interaction.user  # type: ignore[assignment]
        async with session_scope() as db:
            row = await ensure_member(db, interaction.guild_id, member)
            row.afk_message, row.afk_since = reason or None, utcnow()
        self.afk[(interaction.guild_id, member.id)] = (reason, utcnow())
        cfg = await config.get(interaction.guild_id, "afk")
        if cfg.get("nick_prefix", True) and not member.display_name.startswith("[AFK]") and member.top_role < interaction.guild.me.top_role \
                and member.id != interaction.guild.owner_id:
            try:
                await member.edit(nick=f"[AFK] {member.display_name}"[:32])
            except discord.HTTPException:
                pass
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).info(_("afk.set_title"), _("afk.set", reason=reason or "—")), ephemeral=False)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not self.afk:
            return
        gid = message.guild.id
        if not await config.enabled(gid, "afk"):
            return
        _ = None
        key = (gid, message.author.id)
        if key in self.afk and not message.content.startswith("/"):
            _reason, since_dt = self.afk.pop(key)
            async with session_scope() as db:
                row = await db.get(Member, key)
                if row:
                    row.afk_message, row.afk_since = None, None
            member = message.author
            if isinstance(member, discord.Member) and member.display_name.startswith("[AFK] "):
                try:
                    await member.edit(nick=member.display_name[6:] or None)
                except discord.HTTPException:
                    pass
            _ = await i18n.for_guild(gid)
            th = await theme(message.guild)
            try:
                await message.reply(embed=th.success(_("afk.back_title"), _("afk.back", since=ts(since_dt, "R"))), delete_after=10, mention_author=False)
            except discord.HTTPException:
                pass
        mentioned = [m for m in message.mentions if (gid, m.id) in self.afk and m.id != message.author.id][:3]
        if mentioned:
            _ = _ or await i18n.for_guild(gid)
            th = await theme(message.guild)
            lines = []
            for m in mentioned:
                reason, since_dt = self.afk[(gid, m.id)]
                lines.append(_("afk.is_afk", user=m.mention, since=ts(since_dt, "R"), reason=reason or "—"))
            try:
                await message.reply(embed=th.info(_("afk.title"), "\n".join(lines)), delete_after=15, mention_author=False,
                                    allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


# ───────────────────────── Geburtstage ─────────────────────────
class Birthdays(commands.Cog):
    module = "birthday"
    help_category = "community"

    birthday = app_commands.Group(name="birthday", description="Geburtstag verwalten (privat gespeichert)", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @birthday.command(name="set", description="Hinterlege deinen Geburtstag (nur Tag & Monat werden angezeigt)")
    async def set_(self, interaction: discord.Interaction, day: app_commands.Range[int, 1, 31], month: app_commands.Range[int, 1, 12],
                   year: app_commands.Range[int, 1900, 2030] | None = None):
        import calendar
        if day > calendar.monthrange(year or 2024, month)[1]:
            raise UserError("bday.invalid")
        async with session_scope() as db:
            row = await ensure_member(db, interaction.guild_id, interaction.user)
            row.birthday_day, row.birthday_month, row.birthday_year = day, month, year
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("bday.saved", date=f"{day:02d}.{month:02d}.")))

    @birthday.command(name="remove", description="Entfernt deinen Geburtstag")
    async def remove(self, interaction: discord.Interaction):
        async with session_scope() as db:
            row = await db.get(Member, (interaction.guild_id, interaction.user.id))
            if row:
                row.birthday_day = row.birthday_month = row.birthday_year = None
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("bday.removed")))

    @birthday.command(name="upcoming", description="Die nächsten Geburtstage")
    async def upcoming(self, interaction: discord.Interaction):
        gen = await config.get(interaction.guild_id, "general")
        today = local_now(gen.get("timezone")).date()
        async with SessionLocal() as db:
            rows = (await db.execute(select(Member).where(Member.guild_id == interaction.guild_id, Member.in_guild.is_(True),
                                                          Member.birthday_day.is_not(None)))).scalars().all()

        def days_until(r: Member) -> int:
            for add in (0, 1):
                try:
                    d = today.replace(year=today.year + add, month=r.birthday_month, day=r.birthday_day)
                except ValueError:
                    d = today.replace(year=today.year + add, month=3, day=1)
                if d >= today:
                    return (d - today).days
            return 999

        rows = sorted(rows, key=days_until)[:15]
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        # Datenschutz: kein Datum, kein Jahr – nur "heute" / "in X Tagen", und nur für den Fragenden sichtbar
        lines = [f"🎂 <@{r.user_id}> · " + (_("bday.today") if days_until(r) == 0 else _("bday.in_days", days=days_until(r)))
                 for r in rows]
        await reply(interaction, th.embed(_("bday.upcoming_title"), "\n".join(lines) or _("bday.none"), icon=False))

    @tasks.loop(minutes=10)
    async def loop(self):
        for guild in self.bot.guilds:
            if not await config.enabled(guild.id, "birthday"):
                continue
            cfg = await config.get(guild.id, "birthday")
            gen = await config.get(guild.id, "general")
            now = local_now(gen.get("timezone"))
            await self._expire_roles(guild, cfg, now)
            if now.hour < cfg.get("hour", 9):
                continue
            year_key = now.year
            async with SessionLocal() as db:
                rows = (await db.execute(select(Member).where(and_(Member.guild_id == guild.id, Member.in_guild.is_(True),
                                                                   Member.birthday_day == now.day, Member.birthday_month == now.month)))).scalars().all()
            for r in rows:
                if r.birthday_last_celebrated == year_key:
                    continue
                member = guild.get_member(r.user_id)
                async with session_scope() as db:
                    (await db.get(Member, (guild.id, r.user_id))).birthday_last_celebrated = year_key
                if member is None:
                    continue
                await self._celebrate(guild, member, cfg)

    async def _celebrate(self, guild: discord.Guild, member: discord.Member, cfg) -> None:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        channel = guild.get_channel(cfg.id("channel") or 0)
        if isinstance(channel, discord.TextChannel):
            e = th.embed("🎂 Happy Birthday!", fill(cfg.get("message") or "", user=member.mention, username=member.display_name, server=guild.name), icon=False)
            e.set_thumbnail(url=member.display_avatar.url)
            e.colour = discord.Colour.from_str("#ff79c6")
            try:
                await channel.send(content=member.mention, embed=e, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass
        role = guild.get_role(cfg.id("role") or 0)
        if role and role < guild.me.top_role:
            try:
                await member.add_roles(role, reason="Geburtstag")
            except discord.HTTPException:
                pass
        if cfg.get("reward_coins"):
            await progression.add_coins(guild.id, member, int(cfg["reward_coins"]), reason="birthday")
        if cfg.get("reward_xp"):
            await progression.add_xp(self.bot, guild, member, int(cfg["reward_xp"]), use_multiplier=False, announce=False)

    async def _expire_roles(self, guild: discord.Guild, cfg, now) -> None:
        role = guild.get_role(cfg.id("role") or 0)
        if not role:
            return
        for m in role.members:
            async with SessionLocal() as db:
                r = await db.get(Member, (guild.id, m.id))
            if not r or r.birthday_day != now.day or r.birthday_month != now.month:
                try:
                    await m.remove_roles(role, reason="Geburtstag vorbei")
                except discord.HTTPException:
                    pass

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Starboard(bot))
    await bot.add_cog(AFK(bot))
    await bot.add_cog(Birthdays(bot))
