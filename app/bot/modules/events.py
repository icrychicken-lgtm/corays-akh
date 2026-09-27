"""Server-Events: Ankündigung mit Countdown, RSVP-Button, automatische Erinnerungen, Start-Benachrichtigung, Gewinner-Belohnung."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from app.bot.ui import handle_exception, reply
from app.core.embeds import theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import log_event
from app.core.timeutil import ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import EventParticipant, ServerEvent
from app.services import achievements, progression
from app.services.members import ensure_member
from app.services.notify import notify

log = logging.getLogger("nova.events")


class EventButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:ev:(?P<id>\d+):(?P<action>join|list)"):
    def __init__(self, eid: int, action: str, label: str | None = None, disabled: bool = False):
        style, emoji = (discord.ButtonStyle.success, "✋") if action == "join" else (discord.ButtonStyle.secondary, "👥")
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"nova:ev:{eid}:{action}", disabled=disabled))
        self.eid, self.action = eid, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog: Events = interaction.client.get_cog("Events")  # type: ignore[assignment]
        try:
            if self.action == "join":
                await cog.toggle(interaction, self.eid)
            else:
                await cog.participants(interaction, self.eid)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "event")


class Events(commands.Cog):
    module = "events"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    async def embed(self, guild: discord.Guild, ev: ServerEvent) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        async with SessionLocal() as db:
            count = (await db.execute(select(func.count()).select_from(EventParticipant).where(EventParticipant.event_id == ev.id))).scalar_one()
        kind = {"live": "success", "ended": "info", "cancelled": "error"}.get(ev.status, "primary")
        e = th.embed(f"🎉 {ev.name}", ev.description or None, kind=kind, icon=False)
        e.add_field(name=_("ev.when"), value=f"{ts(ev.starts_at, 'F')}\n{ts(ev.starts_at, 'R')}", inline=True)
        e.add_field(name=_("ev.status"), value=_("ev.st." + ev.status), inline=True)
        e.add_field(name=_("ev.participants"), value=f"👥 {count}", inline=True)
        rewards = [x for x in [ev.reward_text, f"🪙 {ev.reward_coins}" if ev.reward_coins else "", f"✨ {ev.reward_xp} XP" if ev.reward_xp else ""] if x]
        if rewards:
            e.add_field(name=_("ev.reward"), value=" · ".join(rewards), inline=False)
        if ev.image_url:
            e.set_image(url=ev.image_url)
        e.set_footer(text=f"Event #{ev.id}", icon_url=th.icon_url)
        return e

    async def view(self, guild: discord.Guild, ev: ServerEvent) -> discord.ui.View:
        _ = await i18n.for_guild(guild.id)
        v = discord.ui.View(timeout=None)
        v.add_item(EventButton(ev.id, "join", _("ev.join"), disabled=ev.status in ("ended", "cancelled")))
        v.add_item(EventButton(ev.id, "list", _("ev.participants")))
        return v

    async def publish(self, guild: discord.Guild, event_id: int) -> ServerEvent:
        cfg = await config.get(guild.id, "events")
        async with session_scope() as db:
            ev = await db.get(ServerEvent, event_id)
            if ev is None or ev.guild_id != guild.id:
                raise UserError("ev.not_found")
            channel = guild.get_channel(ev.channel_id or cfg.id("channel") or 0)
            if not isinstance(channel, discord.TextChannel):
                raise UserError("ev.no_channel")
            ev.channel_id = channel.id
            if ev.message_id:
                try:
                    await channel.get_partial_message(ev.message_id).edit(embed=await self.embed(guild, ev), view=await self.view(guild, ev))
                    return ev
                except discord.NotFound:
                    ev.message_id = None
            role = cfg.id("ping_role")
            msg = await channel.send(content=f"<@&{role}>" if role else None, embed=await self.embed(guild, ev), view=await self.view(guild, ev),
                                     allowed_mentions=discord.AllowedMentions(roles=True))
            ev.message_id = msg.id
        bus.publish(guild.id, "event", {"action": "publish", "id": event_id})
        return ev

    async def refresh(self, guild: discord.Guild, ev: ServerEvent) -> None:
        ch = guild.get_channel(ev.channel_id or 0)
        if isinstance(ch, discord.TextChannel) and ev.message_id:
            try:
                await ch.get_partial_message(ev.message_id).edit(embed=await self.embed(guild, ev), view=await self.view(guild, ev))
            except discord.HTTPException:
                pass

    async def toggle(self, interaction: discord.Interaction, eid: int) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with session_scope() as db:
            ev = await db.get(ServerEvent, eid)
            if ev is None or ev.status in ("ended", "cancelled"):
                raise UserError("ev.closed")
            p = await db.get(EventParticipant, (eid, interaction.user.id))
            if p:
                await db.delete(p)
                msg = th.info(_("ev.left_title"), _("ev.left", name=ev.name))
            else:
                db.add(EventParticipant(event_id=eid, user_id=interaction.user.id))
                msg = th.success(_("ev.joined_title"), _("ev.joined", name=ev.name, when=ts(ev.starts_at, "R")))
        await reply(interaction, msg)
        await self.refresh(interaction.guild, ev)

    async def participants(self, interaction: discord.Interaction, eid: int) -> None:
        async with SessionLocal() as db:
            ev = await db.get(ServerEvent, eid)
            rows = (await db.execute(select(EventParticipant).where(EventParticipant.event_id == eid))).scalars().all()
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        lines = [("🏆 " if r.winner else "• ") + f"<@{r.user_id}>" for r in rows[:80]]
        await reply(interaction, th.embed(f"👥 {ev.name if ev else ''}", "\n".join(lines) or _("ev.no_participants"), icon=False))

    async def finish(self, guild: discord.Guild, event_id: int, winner_ids: list[int], actor: discord.abc.User | None) -> ServerEvent:
        async with session_scope() as db:
            ev = await db.get(ServerEvent, event_id)
            if ev is None or ev.guild_id != guild.id:
                raise UserError("ev.not_found")
            ev.status = "ended"
            for uid in winner_ids:
                p = await db.get(EventParticipant, (event_id, uid))
                if p is None:
                    db.add(EventParticipant(event_id=event_id, user_id=uid, winner=True))
                else:
                    p.winner = True
        for uid in winner_ids:
            member = guild.get_member(uid)
            async with session_scope() as db:
                row = await ensure_member(db, guild.id, member or uid)
                row.events_won += 1
            if ev.reward_coins:
                await progression.add_coins(guild.id, member or uid, ev.reward_coins, reason="event")
            if ev.reward_xp:
                await progression.add_xp(self.bot, guild, member or uid, ev.reward_xp, use_multiplier=False)
            await progression.quest_progress(self.bot, guild, uid, "events_won", 1)
            await achievements.check(self.bot, guild, row, member)
        await self.refresh(guild, ev)
        if winner_ids and ev.channel_id:
            ch = guild.get_channel(ev.channel_id)
            if isinstance(ch, discord.TextChannel):
                _ = await i18n.for_guild(guild.id)
                th = await theme(guild)
                try:
                    await ch.send(embed=th.success(_("ev.winners_title", name=ev.name), " ".join(f"<@{u}>" for u in winner_ids)),
                                  allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException:
                    pass
        await log_event(guild.id, "event", "ended", user=actor, content=ev.name, details={"winners": [str(u) for u in winner_ids]})
        bus.publish(guild.id, "event", {"action": "ended", "id": event_id})
        return ev

    @app_commands.command(name="events", description="Kommende Server-Events")
    @app_commands.guild_only()
    async def events_cmd(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as db:
            rows = (await db.execute(select(ServerEvent).where(ServerEvent.guild_id == interaction.guild_id,
                                                               ServerEvent.status.in_(["scheduled", "live"])).order_by(ServerEvent.starts_at).limit(10))).scalars().all()
        lines = []
        for ev in rows:
            link = f" · [→](https://discord.com/channels/{ev.guild_id}/{ev.channel_id}/{ev.message_id})" if ev.message_id else ""
            lines.append(f"{'🔴' if ev.status == 'live' else '📅'} **{ev.name}** — {ts(ev.starts_at, 'f')} ({ts(ev.starts_at, 'R')}){link}")
        await reply(interaction, th.embed(_("ev.list_title"), "\n".join(lines) or _("ev.list_empty"), icon=False), ephemeral=False)

    @tasks.loop(seconds=60)
    async def loop(self):
        now = utcnow()
        async with SessionLocal() as db:
            rows = (await db.execute(select(ServerEvent).where(ServerEvent.status.in_(["scheduled", "live"]),
                                                               ServerEvent.starts_at <= now + timedelta(days=2)))).scalars().all()
        for ev in rows:
            guild = self.bot.get_guild(ev.guild_id)
            if guild is None or not await config.enabled(guild.id, "events"):
                continue
            cfg = await config.get(guild.id, "events")
            try:
                if ev.status == "scheduled" and ev.starts_at <= now:
                    await self._start(guild, ev, cfg)
                elif ev.status == "live" and now - ev.starts_at > timedelta(hours=12):
                    async with session_scope() as db:
                        (await db.get(ServerEvent, ev.id)).status = "ended"
                elif ev.status == "scheduled":
                    await self._remind(guild, ev, cfg, now)
            except Exception:  # noqa: BLE001
                log.exception("Event-Loop Fehler (%s)", ev.id)

    async def _remind(self, guild: discord.Guild, ev: ServerEvent, cfg, now) -> None:
        offsets = sorted({int(x) for x in cfg.get("reminders") or [] if str(x).strip().isdigit()}, reverse=True)
        for off in offsets:
            if str(off) in (ev.reminders_sent or []):
                continue
            if ev.starts_at - timedelta(minutes=off) <= now:
                async with session_scope() as db:
                    row = await db.get(ServerEvent, ev.id)
                    row.reminders_sent = [*(row.reminders_sent or []), *[str(o) for o in offsets if o >= off]]
                _ = await i18n.for_guild(guild.id)
                th = await theme(guild)
                e = th.info(_("ev.reminder_title", name=ev.name), _("ev.reminder", when=ts(ev.starts_at, "R")))
                ch = guild.get_channel(ev.channel_id or 0)
                if isinstance(ch, discord.TextChannel):
                    role = cfg.id("ping_role")
                    try:
                        await ch.send(content=f"<@&{role}>" if role else None, embed=e, allowed_mentions=discord.AllowedMentions(roles=True))
                    except discord.HTTPException:
                        pass
                if cfg.get("dm_participants", True):
                    async with SessionLocal() as db:
                        uids = (await db.execute(select(EventParticipant.user_id).where(EventParticipant.event_id == ev.id))).scalars().all()
                    for uid in uids[:200]:
                        m = guild.get_member(uid)
                        if m:
                            try:
                                await m.send(embed=e)
                            except discord.HTTPException:
                                pass
                break

    async def _start(self, guild: discord.Guild, ev: ServerEvent, cfg) -> None:
        async with session_scope() as db:
            row = await db.get(ServerEvent, ev.id)
            row.status = "live"
            ev = row
        await self.refresh(guild, ev)
        link = f"https://discord.com/channels/{guild.id}/{ev.channel_id}/{ev.message_id}" if ev.message_id else ""
        await notify(guild, "event_start", event=ev.name, url=link, server=guild.name)
        bus.publish(guild.id, "event", {"action": "live", "id": ev.id})

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(EventButton)
    await bot.add_cog(Events(bot))
