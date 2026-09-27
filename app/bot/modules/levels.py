"""Levelsystem-Commands: /level, /leaderboard, /xp (Admin)."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from app.bot.ui import Paginator, chunk, reply
from app.core.embeds import fmt_num, progress_bar, theme
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import audit
from app.db.base import SessionLocal
from app.db.models import Member
from app.services import progression

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


async def rank_of(guild_id: int, xp: int) -> int:
    async with SessionLocal() as s:
        higher = (await s.execute(select(func.count()).select_from(Member).where(
            Member.guild_id == guild_id, Member.xp > xp, Member.in_guild.is_(True)))).scalar_one()
    return higher + 1


@app_commands.guild_only()
class Levels(commands.Cog):
    module = "levels"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="level", description="Zeigt Level, XP und Rang")
    async def level(self, interaction: discord.Interaction, user: discord.Member | None = None):
        member = user or interaction.user
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as s:
            row = await s.get(Member, (interaction.guild_id, member.id))
        xp = row.xp if row else 0
        level, into, need = progression.level_from_xp(xp)
        rank = await rank_of(interaction.guild_id, xp)
        pct = into / need * 100 if need else 0
        e = th.embed(_("levels.card_title", user=member.display_name), user=member, icon=False)
        e.set_thumbnail(url=member.display_avatar.url)
        e.add_field(name=_("levels.f_level"), value=f"**{level}**", inline=True)
        e.add_field(name=_("levels.f_rank"), value=f"**#{rank}**", inline=True)
        e.add_field(name=_("levels.f_total"), value=f"**{fmt_num(xp)}** XP", inline=True)
        e.add_field(name=_("levels.f_progress", level=level + 1),
                    value=f"{progress_bar(into, need, 18)}\n`{fmt_num(into)} / {fmt_num(need)} XP` · **{pct:.0f}%**", inline=False)
        if row:
            e.add_field(name="💬", value=fmt_num(row.messages), inline=True)
            e.add_field(name="🎙️", value=f"{row.voice_minutes // 60}h {row.voice_minutes % 60}m", inline=True)
            e.add_field(name="😀", value=fmt_num(row.reactions), inline=True)
        e.set_footer(text=_("levels.how_hint"))
        await reply(interaction, e, ephemeral=False)

    @app_commands.command(name="leaderboard", description="Bestenliste: XP, Coins, Voice oder Nachrichten", extras={"module": "core"})
    @app_commands.choices(board=[
        app_commands.Choice(name="⭐ XP / Level", value="xp"), app_commands.Choice(name="💰 Coins", value="coins"),
        app_commands.Choice(name="🎙️ Voice", value="voice_minutes"), app_commands.Choice(name="💬 Nachrichten", value="messages"),
    ])
    async def leaderboard(self, interaction: discord.Interaction, board: app_commands.Choice[str] | None = None):
        key = board.value if board else "xp"
        if key == "coins" and not await config.enabled(interaction.guild_id, "economy"):
            key = "xp"
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        eco = await config.get(interaction.guild_id, "economy")
        col = getattr(Member, key)
        async with SessionLocal() as s:
            rows = (await s.execute(select(Member).where(Member.guild_id == interaction.guild_id, Member.in_guild.is_(True), col > 0)
                                    .order_by(col.desc()).limit(100))).scalars().all()
        if not rows:
            await reply(interaction, th.info(_("levels.lb_title"), _("levels.lb_empty")), ephemeral=False)
            return

        def fmt(r: Member) -> str:
            if key == "xp":
                return f"Level **{r.level}** · {fmt_num(r.xp)} XP"
            if key == "coins":
                return f"{eco.get('currency_emoji')} **{fmt_num(r.coins)}**"
            if key == "voice_minutes":
                return f"🎙️ **{r.voice_minutes // 60}h {r.voice_minutes % 60}m**"
            return f"💬 **{fmt_num(r.messages)}**"

        me_pos = next((i for i, r in enumerate(rows, 1) if r.user_id == interaction.user.id), None)
        pages = []
        title = _("levels.lb_title") + " · " + {"xp": "XP", "coins": eco.get("currency_name", "Coins"), "voice_minutes": "Voice", "messages": _("levels.messages")}[key]
        for pi, group in enumerate(chunk(rows, 10)):
            lines = []
            for i, r in enumerate(group, pi * 10 + 1):
                lines.append(f"{MEDALS.get(i, f'`#{i:>2}`')} <@{r.user_id}> — {fmt(r)}")
            e = th.embed(title, "\n".join(lines), icon=False)
            if interaction.guild.icon:
                e.set_thumbnail(url=interaction.guild.icon.url)
            if me_pos:
                e.set_footer(text=_("levels.lb_your_rank", rank=me_pos), icon_url=interaction.user.display_avatar.url)
            pages.append(e)
        await Paginator(interaction.user.id, pages).send(interaction)

    xp = app_commands.Group(name="xp", description="XP verwalten (Admin)", guild_only=True,
                            default_permissions=discord.Permissions(manage_guild=True))

    @xp.command(name="give", description="XP vergeben")
    async def xp_give(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 10_000_000]):
        await self._xp_change(interaction, user, amount)

    @xp.command(name="take", description="XP abziehen")
    async def xp_take(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, 10_000_000]):
        await self._xp_change(interaction, user, -amount)

    @xp.command(name="set", description="XP exakt setzen")
    async def xp_set(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 0, 1_000_000_000]):
        row = await progression.set_xp(self.bot, interaction.guild, user.id, amount)
        await audit(interaction.guild_id, interaction.user.id, str(interaction.user), "xp.set", str(user), {"xp": amount}, source="bot")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await reply(interaction, th.success(_("common.success"), _("levels.xp_set", user=user.mention, xp=fmt_num(row.xp), level=row.level)))

    async def _xp_change(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        row = await progression.add_xp(self.bot, interaction.guild, user, amount, use_multiplier=False, announce=False)
        await audit(interaction.guild_id, interaction.user.id, str(interaction.user), "xp.change", str(user), {"delta": amount}, source="bot")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await reply(interaction, th.success(_("common.success"), _("levels.xp_set", user=user.mention, xp=fmt_num(row.xp if row else 0), level=row.level if row else 0)))


async def setup(bot: commands.Bot):
    await bot.add_cog(Levels(bot))
