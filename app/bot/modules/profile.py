"""User-Profile: Level, XP, Coins, Achievements, Badges, Games, Aktivität, Profil-Banner."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.bot.ui import reply
from app.core.embeds import divider, fmt_num, progress_bar, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.schema import URL_RE
from app.core.timeutil import ts
from app.db.base import SessionLocal, session_scope
from app.db.models import MemberAchievement
from app.services import achievements, progression
from app.services.members import ensure_member


@app_commands.guild_only()
class Profile(commands.Cog):
    module = "profiles"
    help_category = "community"

    profile_group = app_commands.Group(name="profil", description="Profil anpassen", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def build(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        eco = await config.get(guild.id, "economy")
        async with session_scope() as db:
            row = await ensure_member(db, guild.id, member)
        async with SessionLocal() as db:
            unlocked = set((await db.execute(select(MemberAchievement.key).where(
                MemberAchievement.guild_id == guild.id, MemberAchievement.user_id == member.id))).scalars())
        badges = await achievements.badges_for(guild, row, member)
        level, into, need = progression.level_from_xp(row.xp)
        e = th.embed(None, None, icon=False)
        e.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        e.set_thumbnail(url=member.display_avatar.with_size(256).url)
        header = f"{divider()}\n**{member.mention}**"
        if row.profile_bio:
            header += f"\n*{row.profile_bio}*"
        if badges:
            header += "\n" + " ".join(f"{b.emoji}" for b in badges[:15])
        e.description = header + f"\n{divider()}"
        e.add_field(name="⭐ Level", value=f"**{level}**", inline=True)
        e.add_field(name="✨ XP", value=fmt_num(row.xp), inline=True)
        if await config.enabled(guild.id, "economy"):
            e.add_field(name=f"{eco.get('currency_emoji')} {eco.get('currency_name')}", value=fmt_num(row.coins), inline=True)
        e.add_field(name=_("profile.progress"), value=f"{progress_bar(into, need, 16)} `{into}/{need}`", inline=False)
        e.add_field(name="🏆 Achievements", value=f"{len(unlocked)} / {len(achievements.ACHIEVEMENTS)}", inline=True)
        e.add_field(name="🎖️ Badges", value=", ".join(b.name for b in badges[:6]) or "—", inline=True)
        e.add_field(name="💬 " + _("profile.messages"), value=fmt_num(row.messages), inline=True)
        e.add_field(name="🎙️ " + _("profile.voice"), value=f"{row.voice_minutes // 60}h {row.voice_minutes % 60}m", inline=True)
        e.add_field(name="🔥 Streak", value=f"{row.daily_streak} ({_('profile.best')} {row.daily_best_streak})", inline=True)
        if member.joined_at:
            e.add_field(name="📅 " + _("profile.joined"), value=ts(member.joined_at, "D"), inline=True)
        from app.bot.modules.gangs import gang_of
        gang = await gang_of(guild.id, member.id)
        if gang:
            e.add_field(name="🏴 Gang", value=f"{gang[0].emoji} **{gang[0].name}** `[{gang[0].tag}]`", inline=True)
        pcfg = await config.get(guild.id, "profiles")
        if pcfg.get("track_games", True) and row.games:
            top = sorted(row.games.items(), key=lambda kv: -kv[1])[:3]
            e.add_field(name="🎮 Games", value=" · ".join(g for g, _n in top), inline=False)
        if row.profile_banner and pcfg.get("allow_banner", True):
            e.set_image(url=row.profile_banner)
        return e

    @app_commands.command(name="profile", description="Zeigt das Community-Profil eines Users")
    async def profile(self, interaction: discord.Interaction, user: discord.Member | None = None):
        await reply(interaction, await self.build(interaction.guild, user or interaction.user), ephemeral=False)  # type: ignore[arg-type]

    @app_commands.command(name="achievements", description="Achievements und Fortschritt")
    async def achievements_cmd(self, interaction: discord.Interaction, user: discord.Member | None = None):
        member = user or interaction.user
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with session_scope() as db:
            row = await ensure_member(db, interaction.guild_id, member)
        await achievements.check(self.bot, interaction.guild, row, member)  # type: ignore[arg-type]
        async with SessionLocal() as db:
            unlocked = set((await db.execute(select(MemberAchievement.key).where(
                MemberAchievement.guild_id == interaction.guild_id, MemberAchievement.user_id == member.id))).scalars())
        lines = []
        for p in achievements.progress_for(row, unlocked):
            name = _(f"ach.{p['key']}.name")
            if p["unlocked"]:
                lines.append(f"{p['emoji']} **{name}** ✅")
            else:
                lines.append(f"🔒 {name} · {progress_bar(p['value'], p['target'], 8)} `{fmt_num(p['value'])}/{fmt_num(p['target'])}`")
        e = th.embed(_("profile.ach_title", user=member.display_name), "\n".join(lines), user=member, icon=False)
        await reply(interaction, e)

    @profile_group.command(name="banner", description="Setzt dein Profil-Banner (Bild-URL, leer = entfernen)")
    async def banner(self, interaction: discord.Interaction, url: str = ""):
        pcfg = await config.get(interaction.guild_id, "profiles")
        if not pcfg.get("allow_banner", True):
            raise UserError("profile.banner_disabled")
        if url and (not URL_RE.match(url) or not url.lower().split("?")[0].endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))):
            raise UserError("profile.banner_invalid")
        async with session_scope() as db:
            row = await ensure_member(db, interaction.guild_id, interaction.user)
            row.profile_banner = url or None
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("common.done")))

    @profile_group.command(name="bio", description="Setzt deine Profil-Bio")
    async def bio(self, interaction: discord.Interaction, text: app_commands.Range[str, 0, 200] = ""):
        async with session_scope() as db:
            row = await ensure_member(db, interaction.guild_id, interaction.user)
            row.profile_bio = text or None
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("common.done")))


async def setup(bot: commands.Bot):
    cog = Profile(bot)
    await bot.add_cog(cog)

    @app_commands.context_menu(name="Profil anzeigen")
    @app_commands.guild_only()
    async def ctx_profile(interaction: discord.Interaction, user: discord.Member):
        await reply(interaction, await cog.build(interaction.guild, user))

    ctx_profile.extras["module"] = "profiles"
    bot.tree.add_command(ctx_profile)
