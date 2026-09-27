"""Reminders: /remind 2h Stream beginnt → Erinnerung per DM (Fallback: Channel). /reminders listet und löscht."""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from app.bot.ui import BaseView, reply
from app.core.embeds import theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.timeutil import parse_duration, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Reminder

log = logging.getLogger("nova.reminders")


class ReminderDelete(discord.ui.Select):
    def __init__(self, rows: list[Reminder], placeholder: str):
        super().__init__(placeholder=placeholder, options=[
            discord.SelectOption(label=r.content[:90], value=str(r.id), description=r.remind_at.strftime("%d.%m.%Y %H:%M UTC")) for r in rows[:25]])

    async def callback(self, interaction: discord.Interaction):
        async with session_scope() as db:
            r = await db.get(Reminder, int(self.values[0]))
            if r and r.user_id == interaction.user.id:
                await db.delete(r)
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.edit_message(embed=(await theme(interaction.guild)).success(_("common.success"), _("remind.deleted")), view=None)


class Reminders(commands.Cog):
    module = "reminders"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    @app_commands.command(name="remind", description="Erinnert dich per DM, z. B. /remind 2h Stream beginnt")
    @app_commands.describe(time="z. B. 10m, 2h, 1d12h", text="Woran soll ich dich erinnern?")
    async def remind(self, interaction: discord.Interaction, time: str, text: app_commands.Range[str, 1, 1000]):
        secs = parse_duration(time)
        if not secs or secs < 30 or secs > 365 * 86400:
            raise UserError("remind.err_time")
        limit = 25
        if interaction.guild_id:
            limit = (await config.get(interaction.guild_id, "reminders")).get("max_per_user", 25)
        async with session_scope() as db:
            count = (await db.execute(select(func.count()).select_from(Reminder).where(Reminder.user_id == interaction.user.id, Reminder.done.is_(False)))).scalar_one()
            if count >= limit:
                raise UserError("remind.err_limit", max=limit)
            r = Reminder(user_id=interaction.user.id, guild_id=interaction.guild_id, channel_id=interaction.channel_id, content=text,
                         remind_at=utcnow() + timedelta(seconds=secs), done=False)
            db.add(r)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await reply(interaction, th.success(_("remind.set_title"), _("remind.set", when=ts(r.remind_at, "R"), text=text)))

    @app_commands.command(name="reminders", description="Deine aktiven Erinnerungen anzeigen und löschen")
    async def reminders(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as db:
            rows = (await db.execute(select(Reminder).where(Reminder.user_id == interaction.user.id, Reminder.done.is_(False))
                                     .order_by(Reminder.remind_at).limit(25))).scalars().all()
        if not rows:
            await reply(interaction, th.info(_("remind.list_title"), _("remind.list_empty")))
            return
        e = th.embed(_("remind.list_title"), "\n".join(f"⏰ {ts(r.remind_at, 'R')} — {r.content[:120]}" for r in rows), icon=False)
        view = BaseView(owner_id=interaction.user.id)
        view.add_item(ReminderDelete(list(rows), _("remind.delete_placeholder")))
        await reply(interaction, e, view=view)

    @tasks.loop(seconds=20)
    async def loop(self):
        async with session_scope() as db:
            due = (await db.execute(select(Reminder).where(Reminder.done.is_(False), Reminder.remind_at <= utcnow()).limit(50))).scalars().all()
            for r in due:
                r.done = True
            items = [(r.id, r.user_id, r.guild_id, r.channel_id, r.content, r.created_at) for r in due]
        for _id, uid, gid, cid, content, created in items:
            guild = self.bot.get_guild(gid) if gid else None
            _ = await i18n.for_guild(gid)
            th = await theme(guild)
            e = th.info(_("remind.title"), content)
            e.add_field(name=_("remind.created"), value=ts(created, "R"))
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                await user.send(embed=e)
            except discord.HTTPException:
                ch = self.bot.get_channel(cid) if cid else None
                if isinstance(ch, (discord.TextChannel, discord.Thread)):
                    try:
                        await ch.send(content=f"<@{uid}>", embed=e, allowed_mentions=discord.AllowedMentions(users=True))
                    except discord.HTTPException:
                        pass

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
