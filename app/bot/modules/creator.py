"""Creator-/Media-Funktionen für Streamer auf dem Server:
Media-Profil & Socials, eigene Live-Nachricht, Pause, persönliche Statistik, Wochenbericht per DM, Media des Monats,
Stream-Talk-Thread zum Live-Post, Verifiziert-Rolle, Collab-Börse, Raid-Ansagen und der Twitch-Chat-Wächter
(Chat-Statistik nach dem Stream + Warnwort-Alarm für Mods)."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select

from app.bot.ui import handle_exception, reply
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import capture_error, log_event
from app.core.timeutil import human_duration, local_now, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Streamer, StreamAlert, StreamSession
from app.services import twitch_chat

log = logging.getLogger("nova.creator")
SOCIALS = [("tiktok", "TikTok", "🎵"), ("youtube", "YouTube", "▶️"), ("instagram", "Instagram", "📸"), ("x", "X / Twitter", "𝕏"), ("other", "Link", "🔗")]


def stream_url(s: Streamer) -> str:
    return {"twitch": f"https://twitch.tv/{s.channel}", "kick": f"https://kick.com/{s.channel}",
            "youtube": f"https://youtube.com/channel/{s.platform_user_id or s.channel}"}[s.platform]


def session_stats(sessions: list[StreamSession]) -> dict:
    secs = sum(((x.ended_at or utcnow()) - x.started_at).total_seconds() for x in sessions)
    return {"count": len(sessions), "secs": secs, "peak": max((x.peak_viewers for x in sessions), default=0),
            "avg": round(sum(x.avg_viewers for x in sessions) / len(sessions)) if sessions else 0,
            "checkins": sum(x.checkins for x in sessions)}


async def is_media(member: discord.Member) -> bool:
    from app.bot.modules.streamhub import can_host
    cfg = await config.get(member.guild.id, "streamer")
    role_id = cfg.id("media_role")
    return bool(role_id and member.get_role(role_id)) or await can_host(member)


async def own_streamer(guild_id: int, user_id: int) -> Streamer:
    async with SessionLocal() as db:
        s = (await db.execute(select(Streamer).where(Streamer.guild_id == guild_id, Streamer.discord_user_id == user_id)
                              .order_by(Streamer.id))).scalars().first()
    if s is None:
        raise UserError("cr.not_connected")
    return s


# ───────────────────────── Formulare ─────────────────────────
class LinksModal(discord.ui.Modal):
    def __init__(self, _, current: dict):
        super().__init__(title=_("cr.links_modal")[:45])
        self.inputs = {}
        for key, label, _emoji in SOCIALS:
            ti = discord.ui.TextInput(label=label, placeholder="https://…", required=False, max_length=200, default=current.get(key) or None)
            self.inputs[key] = ti
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            _ = await i18n.for_guild(interaction.guild_id)
            data = {}
            for key, ti in self.inputs.items():
                v = ti.value.strip()
                if v and not v.startswith(("http://", "https://")):
                    v = "https://" + v
                if v:
                    data[key] = v[:200]
            await config.set_state(f"cr:l:{interaction.guild_id}:{interaction.user.id}", data)
            await reply(interaction, (await theme(interaction.guild)).success(_("cr.links_saved_title"), _("cr.links_saved", count=len(data))))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "media_links")


class MessageModal(discord.ui.Modal):
    def __init__(self, _, streamer_id: int, current: str):
        super().__init__(title=_("cr.msg_modal")[:45])
        self.streamer_id = streamer_id
        self.text = discord.ui.TextInput(label=_("cr.msg_label")[:45], style=discord.TextStyle.paragraph, required=False, max_length=500,
                                         placeholder=_("cr.msg_placeholder")[:100], default=current or None)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            _ = await i18n.for_guild(interaction.guild_id)
            async with session_scope() as db:
                row = await db.get(Streamer, self.streamer_id)
                row.message_template = self.text.value.strip()[:500]
            key = "cr.msg_saved" if self.text.value.strip() else "cr.msg_reset"
            await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _(key)))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "media_message")


class CollabModal(discord.ui.Modal):
    def __init__(self, _):
        super().__init__(title=_("cr.collab_modal")[:45])
        self.game = discord.ui.TextInput(label=_("cr.collab_game")[:45], max_length=80, placeholder="Fortnite, GTA RP, Valorant …")
        self.when = discord.ui.TextInput(label=_("cr.collab_when")[:45], max_length=80, placeholder=_("cr.collab_when_ph")[:100])
        self.slots = discord.ui.TextInput(label=_("cr.collab_slots")[:45], max_length=2, default="2")
        self.info = discord.ui.TextInput(label=_("cr.collab_info")[:45], style=discord.TextStyle.paragraph, required=False, max_length=500)
        for ti in (self.game, self.when, self.slots, self.info):
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            cog: Creator = interaction.client.get_cog("Creator")  # type: ignore[assignment]
            slots = int(re.sub(r"\D", "", self.slots.value) or 2)
            await cog.post_collab(interaction, self.game.value, self.when.value, max(1, min(slots, 20)), self.info.value)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "collab")


class CollabJoin(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:collab:(?P<owner>\d+):(?P<slots>\d+)"):
    def __init__(self, owner: int, slots: int, label: str | None = None, disabled: bool = False):
        super().__init__(discord.ui.Button(label=label, emoji="🤝", style=discord.ButtonStyle.primary, disabled=disabled,
                                           custom_id=f"nova:collab:{owner}:{slots}"))
        self.owner, self.slots = owner, slots

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["owner"]), int(match["slots"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            _ = await i18n.for_guild(interaction.guild_id)
            if interaction.user.id == self.owner:
                raise UserError("cr.collab_own")
            e = interaction.message.embeds[0]
            field = next((i for i, f in enumerate(e.fields) if f.name.startswith("👥")), None)
            names = [] if field is None or e.fields[field].value == "—" else e.fields[field].value.split("\n")
            if interaction.user.mention in names:
                raise UserError("cr.collab_already")
            if len(names) >= self.slots:
                raise UserError("cr.collab_full")
            names.append(interaction.user.mention)
            e.set_field_at(field, name=f"👥 {len(names)}/{self.slots}", value="\n".join(names), inline=False)
            view = discord.ui.View(timeout=None)
            view.add_item(CollabJoin(self.owner, self.slots, _("cr.collab_join"), disabled=len(names) >= self.slots))
            await interaction.response.edit_message(embed=e, view=view)
            owner = interaction.guild.get_member(self.owner)
            if owner:
                try:
                    await owner.send(embed=(await theme(interaction.guild)).success(
                        _("cr.collab_dm_title"), _("cr.collab_dm", user=interaction.user.mention, name=interaction.user.display_name, url=interaction.message.jump_url)))
                except discord.HTTPException:
                    pass
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "collab_join")


# ───────────────────────── Cog ─────────────────────────
class Creator(commands.Cog, name="Creator"):
    module = "streamer"
    help_category = "streamer"

    media = app_commands.Group(name="media", description="Für Streamer/Medias: Profil, Socials, Live-Nachricht, Statistik", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._watch: dict[int, asyncio.Task] = {}      # streamer_id → Chat-Wächter
        self._chat: dict[int, dict] = {}               # streamer_id → Chat-Statistik
        self._alerted: dict[tuple[int, str], float] = {}
        self.weekly.start()

    def cog_unload(self):
        self.weekly.cancel()
        for t in self._watch.values():
            t.cancel()

    # ── Hooks aus dem StreamerCog ──
    async def on_connect(self, guild: discord.Guild, member: discord.Member, s: Streamer) -> None:
        cfg = await config.get(guild.id, "streamer")
        role = guild.get_role(cfg.id("media_verified_role") or 0)
        if role and role < guild.me.top_role and role not in member.roles:
            try:
                await member.add_roles(role, reason=f"Twitch verbunden: {s.channel}")
            except discord.HTTPException:
                pass

    async def on_live(self, guild: discord.Guild, s: Streamer, info, sess: StreamSession) -> None:
        cfg = await config.get(guild.id, "streamer")
        async with SessionLocal() as db:
            s = await db.get(Streamer, s.id)
        if cfg.get("live_thread", True) and s.live_message_id and s.live_channel_id:
            ch = guild.get_channel(s.live_channel_id)
            if isinstance(ch, discord.TextChannel):
                _ = await i18n.for_guild(guild.id)
                try:
                    await ch.get_partial_message(s.live_message_id).create_thread(
                        name=_("cr.thread_name", streamer=s.display_name)[:100], auto_archive_duration=1440)
                except discord.HTTPException:
                    pass
        if s.platform == "twitch" and (cfg.get("chat_stats", True) or cfg.get("chat_watch_words")):
            self._start_watch(guild, s)

    async def on_offline(self, guild: discord.Guild, s: Streamer) -> None:
        task = self._watch.pop(s.id, None)
        if task:
            task.cancel()
        stats = self._chat.pop(s.id, None)
        cfg = await config.get(guild.id, "streamer")
        if not stats or not cfg.get("chat_stats", True) or stats["messages"] < 5:
            return
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        top = stats["users"].most_common(5)
        e = discord.Embed(title=_("cr.chat_stats_title", streamer=s.display_name), colour=th.color("primary"))
        e.add_field(name=_("cr.f_messages"), value=fmt_num(stats["messages"]), inline=True)
        e.add_field(name=_("cr.f_chatters"), value=fmt_num(len(stats["users"])), inline=True)
        e.add_field(name=_("cr.f_first"), value=fmt_num(stats["first"]), inline=True)
        if top:
            medals = ["🥇", "🥈", "🥉", "4.", "5."]
            e.add_field(name=_("cr.f_top_chatters"), value="\n".join(f"{medals[i]} **{n}** · {fmt_num(c)}" for i, (n, c) in enumerate(top)), inline=False)
        if stats["emotes"]:
            e.add_field(name=_("cr.f_top_emotes"), value=" · ".join(f"`{n}` {c}×" for n, c in stats["emotes"].most_common(5)), inline=False)
        ch = guild.get_channel(s.announce_channel_id or cfg.id("default_channel") or 0)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(embed=e)
            except discord.HTTPException:
                pass

    # ── Twitch-Chat-Wächter ──
    def _start_watch(self, guild: discord.Guild, s: Streamer) -> None:
        if s.id in self._watch and not self._watch[s.id].done():
            return
        self._chat[s.id] = {"messages": 0, "users": Counter(), "emotes": Counter(), "first": 0}
        self._watch[s.id] = asyncio.create_task(self._watch_chat(guild.id, s.id, s.channel, s.display_name))

    async def _watch_chat(self, guild_id: int, streamer_id: int, login: str, name: str) -> None:
        async def handle(tags: dict[str, str], text: str) -> bool:
            stats = self._chat.get(streamer_id)
            if stats is None:
                return True
            user = tags.get("display-name") or "?"
            stats["messages"] += 1
            stats["users"][user] += 1
            if tags.get("first-msg") == "1":
                stats["first"] += 1
            for part in (tags.get("emotes") or "").split("/"):
                eid, _, pos = part.partition(":")
                if pos:
                    start, _, end = pos.split(",")[0].partition("-")
                    try:
                        stats["emotes"][text[int(start):int(end) + 1]] += 1
                    except ValueError:
                        pass
            await self._check_words(guild_id, login, name, user, tags, text)
            return False

        try:
            await twitch_chat.read_chat(login, handle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await capture_error(exc, guild_id=guild_id, command="creator.chat_watch")

    async def _check_words(self, guild_id: int, login: str, name: str, user: str, tags: dict, text: str) -> None:
        cfg = await config.get(guild_id, "streamer")
        words = [w.lower() for w in cfg.get("chat_watch_words") or [] if w.strip()]
        low = text.lower()
        hit = next((w for w in words if w in low), None)
        if hit is None:
            return
        key = (guild_id, user.lower())
        if time.monotonic() - self._alerted.get(key, 0) < 60:
            return
        self._alerted[key] = time.monotonic()
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        from app.bot.modules.modtools import mod_channel
        ch = guild.get_channel(cfg.id("chat_watch_channel") or 0) or await mod_channel(guild)
        if not isinstance(ch, discord.TextChannel):
            return
        _ = await i18n.for_guild(guild_id)
        e = discord.Embed(title=_("cr.watch_title", streamer=name), description=text[:1500], colour=discord.Colour.orange(), timestamp=utcnow())
        e.add_field(name=_("cr.watch_user"), value=f"**{user}**", inline=True)
        e.add_field(name=_("cr.watch_word"), value=f"`{hit}`", inline=True)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=_("cr.watch_card"), url=f"https://www.twitch.tv/popout/{login}/viewercard/{user.lower()}", emoji="🪪"))
        view.add_item(discord.ui.Button(label=_("cr.watch_chat"), url=f"https://www.twitch.tv/popout/{login}/chat", emoji="💬"))
        try:
            await ch.send(embed=e, view=view)
        except discord.HTTPException:
            pass

    # ── Wochenbericht & Media des Monats ──
    @tasks.loop(minutes=10)
    async def weekly(self):
        for guild in list(self.bot.guilds):
            try:
                if not await config.enabled(guild.id, "streamer"):
                    continue
                gcfg = await config.get(guild.id, "general")
                now = local_now(gcfg.get("timezone"))
                cfg = await config.get(guild.id, "streamer")
                week = f"{now.isocalendar()[0]}-{now.isocalendar()[1]}"
                if cfg.get("media_weekly_dm", True) and now.weekday() == 0 and now.hour >= 10 and await config.state(f"cr:wk:{guild.id}") != week:
                    await config.set_state(f"cr:wk:{guild.id}", week)
                    await self._weekly_reports(guild)
                month = f"{now.year}-{now.month}"
                if cfg.get("media_of_month", True) and now.day == 1 and now.hour >= 12 and await config.state(f"cr:mo:{guild.id}") != month:
                    await config.set_state(f"cr:mo:{guild.id}", month)
                    await self._media_of_month(guild)
            except Exception as exc:  # noqa: BLE001
                await capture_error(exc, guild_id=guild.id, command="creator.weekly")

    @weekly.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _period(self, guild_id: int, days: int) -> dict[int, tuple[Streamer, list[StreamSession]]]:
        since = utcnow() - timedelta(days=days)
        async with SessionLocal() as db:
            rows = (await db.execute(select(StreamSession, Streamer).join(Streamer, Streamer.id == StreamSession.streamer_id)
                                     .where(StreamSession.guild_id == guild_id, StreamSession.started_at >= since))).all()
        out: dict[int, tuple[Streamer, list[StreamSession]]] = {}
        for sess, s in rows:
            out.setdefault(s.id, (s, []))[1].append(sess)
        return out

    async def _weekly_reports(self, guild: discord.Guild) -> None:
        _ = await i18n.for_guild(guild.id)
        lang = await i18n.lang(guild.id)
        th = await theme(guild)
        data = await self._period(guild.id, 7)
        async with SessionLocal() as db:
            medias = (await db.execute(select(Streamer).where(Streamer.guild_id == guild.id, Streamer.discord_user_id.is_not(None)))).scalars().all()
        for s in medias:
            member = guild.get_member(s.discord_user_id)
            if member is None:
                continue
            st = session_stats(data.get(s.id, (s, []))[1])
            e = th.embed(_("cr.weekly_title", server=guild.name), _("cr.weekly_none") if not st["count"] else None, icon=False)
            if st["count"]:
                e.add_field(name=_("sh.f_streams"), value=str(st["count"]), inline=True)
                e.add_field(name=_("sh.f_hours"), value=human_duration(st["secs"], lang), inline=True)
                e.add_field(name=_("stream.f_peak"), value=fmt_num(st["peak"]), inline=True)
                e.add_field(name=_("stream.f_avg"), value=fmt_num(st["avg"]), inline=True)
                e.add_field(name=_("sh.f_checkins"), value=fmt_num(st["checkins"]), inline=True)
            if s.avatar_url:
                e.set_thumbnail(url=s.avatar_url)
            e.set_footer(text=_("cr.weekly_footer"))
            try:
                await member.send(embed=e)
            except discord.HTTPException:
                pass

    async def _media_of_month(self, guild: discord.Guild) -> None:
        cfg = await config.get(guild.id, "streamer")
        ch = guild.get_channel(cfg.id("media_month_channel") or cfg.id("default_channel") or 0)
        if not isinstance(ch, discord.TextChannel):
            return
        data = await self._period(guild.id, 30)
        if not data:
            return
        _ = await i18n.for_guild(guild.id)
        lang = await i18n.lang(guild.id)
        th = await theme(guild)
        ranked = sorted(((s, session_stats(x)) for s, x in data.values()), key=lambda t: (-t[1]["secs"], -t[1]["peak"]))
        best, st = ranked[0]
        e = discord.Embed(title=_("cr.month_title"), url=stream_url(best),
                          description=_("cr.month_text", streamer=best.display_name, mention=f"<@{best.discord_user_id}>" if best.discord_user_id else best.display_name),
                          colour=th.color("primary"))
        if best.avatar_url:
            e.set_thumbnail(url=best.avatar_url)
        e.add_field(name=_("sh.f_hours"), value=human_duration(st["secs"], lang), inline=True)
        e.add_field(name=_("sh.f_streams"), value=str(st["count"]), inline=True)
        e.add_field(name=_("stream.f_peak"), value=fmt_num(st["peak"]), inline=True)
        if len(ranked) > 1:
            e.add_field(name=_("cr.month_runners"), value="\n".join(f"{i + 2}. **{s.display_name}** · {human_duration(x['secs'], lang)}" for i, (s, x) in enumerate(ranked[1:5])), inline=False)
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    # ── /media ──
    @media.command(name="profil", description="Media-Karte: Kanal, Socials, Stream-Statistik")
    @app_commands.describe(user="Anderes Media anzeigen")
    async def media_profile(self, interaction: discord.Interaction, user: discord.Member | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        lang = await i18n.lang(interaction.guild_id)
        th = await theme(interaction.guild)
        target = user or interaction.user
        s = await own_streamer(interaction.guild_id, target.id)
        async with SessionLocal() as db:
            sessions = (await db.execute(select(StreamSession).where(StreamSession.streamer_id == s.id))).scalars().all()
            alarms = len((await db.execute(select(StreamAlert.user_id).where(StreamAlert.streamer_id == s.id))).all())
        st = session_stats(list(sessions))
        links = await config.state(f"cr:l:{interaction.guild_id}:{target.id}") or {}
        e = discord.Embed(title=f"{'🔴 ' if s.is_live else ''}{s.display_name}", url=stream_url(s), colour=th.color("primary"),
                          description=_("sh.live_now") if s.is_live else None)
        e.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        if s.avatar_url:
            e.set_thumbnail(url=s.avatar_url)
        e.add_field(name=_("sh.f_streams"), value=fmt_num(st["count"]), inline=True)
        e.add_field(name=_("sh.f_hours"), value=human_duration(st["secs"], lang) if st["secs"] else "—", inline=True)
        e.add_field(name=_("stream.f_peak"), value=fmt_num(st["peak"]), inline=True)
        e.add_field(name=_("stream.f_avg"), value=fmt_num(st["avg"]), inline=True)
        e.add_field(name=_("sh.f_checkins"), value=fmt_num(st["checkins"]), inline=True)
        e.add_field(name=_("sh.f_alarms"), value=f"🔔 {alarms}", inline=True)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=s.platform.capitalize(), url=stream_url(s), emoji="📺"))
        for key, label, emoji in SOCIALS:
            if links.get(key) and len(view.children) < 5:
                view.add_item(discord.ui.Button(label=label, url=links[key], emoji=emoji))
        if not s.enabled:
            e.set_footer(text=_("cr.paused_footer"))
        await reply(interaction, e, view=view, ephemeral=False)

    @media.command(name="links", description="Deine Socials (TikTok, YouTube, Instagram, X) fürs Media-Profil")
    async def media_links(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        await own_streamer(interaction.guild_id, interaction.user.id)
        current = await config.state(f"cr:l:{interaction.guild_id}:{interaction.user.id}") or {}
        await interaction.response.send_modal(LinksModal(_, current))

    @media.command(name="nachricht", description="Eigene Live-Nachricht für deine Streams (leer = Standard)")
    async def media_message(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        s = await own_streamer(interaction.guild_id, interaction.user.id)
        await interaction.response.send_modal(MessageModal(_, s.id, s.message_template))

    @media.command(name="pause", description="Live-Ankündigungen für deine Streams pausieren oder wieder anschalten")
    async def media_pause(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        s = await own_streamer(interaction.guild_id, interaction.user.id)
        async with session_scope() as db:
            row = await db.get(Streamer, s.id)
            row.enabled = not row.enabled
            enabled = row.enabled
        await log_event(interaction.guild_id, "stream", "media_pause", user=interaction.user, content=str(not enabled))
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("cr.resumed" if enabled else "cr.paused")))

    @media.command(name="statistik", description="Deine Stream-Statistik (nur für dich sichtbar)")
    @app_commands.describe(tage="Zeitraum in Tagen (Standard 30)")
    async def media_stats(self, interaction: discord.Interaction, tage: app_commands.Range[int, 1, 365] = 30):
        _ = await i18n.for_guild(interaction.guild_id)
        lang = await i18n.lang(interaction.guild_id)
        s = await own_streamer(interaction.guild_id, interaction.user.id)
        since = utcnow() - timedelta(days=tage)
        async with SessionLocal() as db:
            sessions = (await db.execute(select(StreamSession).where(StreamSession.streamer_id == s.id, StreamSession.started_at >= since)
                                         .order_by(StreamSession.started_at.desc()))).scalars().all()
        st = session_stats(list(sessions))
        th = await theme(interaction.guild)
        e = th.embed(_("cr.stats_title", days=tage), _("cr.stats_none") if not sessions else None, icon=False)
        if sessions:
            e.add_field(name=_("sh.f_streams"), value=str(st["count"]), inline=True)
            e.add_field(name=_("sh.f_hours"), value=human_duration(st["secs"], lang), inline=True)
            e.add_field(name=_("stream.f_peak"), value=fmt_num(st["peak"]), inline=True)
            e.add_field(name=_("stream.f_avg"), value=fmt_num(st["avg"]), inline=True)
            e.add_field(name=_("sh.f_checkins"), value=fmt_num(st["checkins"]), inline=True)
            games = Counter(x.game for x in sessions if x.game).most_common(3)
            if games:
                e.add_field(name=_("sh.f_games"), value="\n".join(f"• {g} ({n}×)" for g, n in games), inline=True)
            e.add_field(name=_("cr.f_recent"), value="\n".join(
                f"{ts(x.started_at, 'd')} · {human_duration(((x.ended_at or utcnow()) - x.started_at).total_seconds(), lang)} · 👀 {fmt_num(x.peak_viewers)} · {(x.game or '—')[:30]}"
                for x in sessions[:5]), inline=False)
        await reply(interaction, e)

    # ── /collab & /raid ──
    @app_commands.command(name="collab", description="Collab-Partner suchen – andere Streamer können sich eintragen")
    @app_commands.guild_only()
    async def collab(self, interaction: discord.Interaction):
        if not await is_media(interaction.user):  # type: ignore[arg-type]
            cfg = await config.get(interaction.guild_id, "streamer")
            raise UserError("stream.media_only", role=f"<@&{cfg.id('media_role')}>" if cfg.id("media_role") else "Media")
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.send_modal(CollabModal(_))

    async def post_collab(self, interaction: discord.Interaction, game: str, when: str, slots: int, info: str) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        cfg = await config.get(interaction.guild_id, "streamer")
        ch = interaction.guild.get_channel(cfg.id("collab_channel") or 0) or interaction.channel
        th = await theme(interaction.guild)
        e = discord.Embed(title=_("cr.collab_title", game=game), description=info or None, colour=th.color("primary"), timestamp=utcnow())
        e.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        e.add_field(name=_("cr.collab_when"), value=when, inline=True)
        e.add_field(name=_("cr.collab_host"), value=interaction.user.mention, inline=True)
        e.add_field(name=f"👥 0/{slots}", value="—", inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(CollabJoin(interaction.user.id, slots, _("cr.collab_join")))
        msg = await ch.send(embed=e, view=view)
        await reply(interaction, th.success(_("common.success"), _("cr.collab_posted", url=msg.jump_url)))

    @app_commands.command(name="raid", description="Ankündigen, wen du gerade raidest – die Community zieht mit")
    @app_commands.guild_only()
    @app_commands.describe(ziel="Twitch-Name des Ziels", ping="Stream-Rolle pingen")
    async def raid(self, interaction: discord.Interaction, ziel: str, ping: bool = False):
        if not await is_media(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        _ = await i18n.for_guild(interaction.guild_id)
        login = re.sub(r"^(https?://)?(www\.)?twitch\.tv/", "", ziel.strip(), flags=re.I).strip("/@ ").lower()
        if not re.fullmatch(r"[a-z0-9_]{3,25}", login):
            raise UserError("stream.verify_bad_name")
        cfg = await config.get(interaction.guild_id, "streamer")
        th = await theme(interaction.guild)
        url = f"https://twitch.tv/{login}"
        e = discord.Embed(title=_("cr.raid_title", user=interaction.user.display_name.upper(), target=login), url=url,
                          description=_("cr.raid_text", target=login), colour=th.color("primary"), timestamp=utcnow())
        e.set_thumbnail(url=interaction.user.display_avatar.url)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=_("cr.raid_join"), url=url, emoji="⚔️"))
        role_id = cfg.id("default_role")
        ch = interaction.guild.get_channel(cfg.id("default_channel") or 0) or interaction.channel
        await ch.send(content=f"<@&{role_id}>" if ping and role_id else None, embed=e, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
        await log_event(interaction.guild_id, "stream", "raid", user=interaction.user, content=login)
        await reply(interaction, th.success(_("common.success"), _("cr.raid_done", channel=ch.mention)))


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(CollabJoin)
    await bot.add_cog(Creator(bot))
