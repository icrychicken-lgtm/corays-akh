"""Streamer-System: Live-Benachrichtigungen (Twitch/YouTube/Kick), Live-Embed-Updates, Live-Rolle, Channel-Umbenennung,
Stream-Statistiken (Sessions, Peak, Ø), Video/Short-Benachrichtigungen und Stream-Check-ins (XP/Coins/Quests)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select

from app.bot.ui import handle_exception, reply
from app.config import settings
from app.core.embeds import fmt_num, hex_to_color, theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import fill, i18n
from app.core.metrics import metrics
from app.core.records import capture_error, log_event
from app.core.timeutil import human_duration, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Streamer, StreamCheckin, StreamSession, StreamSnapshot
from app.services import achievements, progression
from app.services.integrations import get_credentials
from app.services.members import ensure_member
from app.services.notify import notify
from app.services.streaming import PlatformError, StreamInfo, kick, twitch, youtube

log = logging.getLogger("nova.streamer")
VERIFY_SECONDS = 10 * 60
PLATFORM = {"twitch": ("Twitch", "#9146ff", "🟣"), "youtube": ("YouTube", "#ff0033", "🔴"), "kick": ("Kick", "#53fc18", "🟢")}


class CheckinButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:st:checkin:(?P<sid>\d+)"):
    def __init__(self, session_id: int, label: str | None = None, disabled: bool = False):
        super().__init__(discord.ui.Button(label=label, emoji="🙋", style=discord.ButtonStyle.primary,
                                           custom_id=f"nova:st:checkin:{session_id}", disabled=disabled))
        self.session_id = session_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.client.get_cog("StreamerCog").checkin(interaction, self.session_id)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "stream_checkin")


class TwitchConnectButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:tw:connect"):
    def __init__(self, label: str | None = None):
        super().__init__(discord.ui.Button(label=label, emoji="🟣", style=discord.ButtonStyle.primary, custom_id="nova:tw:connect"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.client.get_cog("StreamerCog").connect_flow(interaction)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "twitch_connect")


class TwitchNameModal(discord.ui.Modal):
    def __init__(self, cog: "StreamerCog", _):
        super().__init__(title=_("stream.verify_modal_title")[:45])
        self.cog = cog
        self.name = discord.ui.TextInput(label=_("stream.verify_modal_label")[:45], placeholder=_("stream.verify_modal_placeholder")[:100],
                                         min_length=3, max_length=80)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.cog.verify_flow(interaction, self.name.value)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "twitch_verify")


class StreamerCog(commands.Cog, name="StreamerCog"):
    module = "streamer"
    help_category = "streamer"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_stats: dict[int, float] = {}
        self._verifying: dict[tuple[int, int], asyncio.Task] = {}
        self.poll_loop.change_interval(seconds=max(60, settings.stream_poll_seconds))
        self.poll_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()
        for task in self._verifying.values():
            task.cancel()

    # ── Embeds ──
    async def live_embed(self, guild: discord.Guild, s: Streamer, info: StreamInfo, session: StreamSession | None) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        cfg = await config.get(guild.id, "streamer")
        pname, color, dot = PLATFORM[s.platform]
        th = await theme(guild)
        style = cfg.get("style", "hood")
        if style == "hood":
            lines = _("stream.hood_lines").split("|")
            line = fill(lines[(session.id if session else s.id) % len(lines)], streamer=s.display_name)
            e = discord.Embed(title=_("stream.hood_title", streamer=s.display_name.upper())[:256], url=info.url,
                              description=f"{line}\n\n**{info.title}**" if info.title else line, colour=th.color("primary"), timestamp=utcnow())
        elif style == "clean":
            e = discord.Embed(title=(info.title or s.display_name)[:256], url=info.url, colour=discord.Colour.from_str("#2b2b30"), timestamp=utcnow())
        else:
            e = discord.Embed(title=f"🔴 LIVE · {info.title or s.display_name}"[:256], url=info.url, colour=hex_to_color(color), timestamp=utcnow())
        e.set_author(name=f"{s.display_name} · {pname}", url=info.url, icon_url=s.avatar_url)
        e.add_field(name=_("stream.f_streamer"), value=f"[{s.display_name}]({info.url})", inline=True)
        e.add_field(name=_("stream.f_game"), value=info.game or "—", inline=True)
        if cfg.get("show_viewers", True):
            e.add_field(name=_("stream.f_viewers"), value=f"👀 {fmt_num(info.viewers)}", inline=True)
        if info.started_at:
            e.add_field(name=_("stream.f_since"), value=ts(info.started_at, "R"), inline=True)
        if session and session.checkins:
            e.add_field(name=_("stream.f_checkins"), value=f"🙋 {session.checkins}", inline=True)
        if info.thumbnail:
            e.set_image(url=info.thumbnail)
        if s.avatar_url:
            e.set_thumbnail(url=s.avatar_url)
        e.set_footer(text=f"{dot} {pname} · {th.footer}", icon_url=th.icon_url)
        return e

    async def summary_embed(self, guild: discord.Guild, s: Streamer, session: StreamSession) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        lang = await i18n.lang(guild.id)
        pname, color, dot = PLATFORM[s.platform]
        e = discord.Embed(title=_("stream.ended_title", name=s.display_name), description=f"**{session.title}**" if session.title else None,
                          colour=discord.Colour.dark_grey(), timestamp=utcnow())
        if s.avatar_url:
            e.set_thumbnail(url=s.avatar_url)
        dur = ((session.ended_at or utcnow()) - session.started_at).total_seconds()
        e.add_field(name=_("stream.f_duration"), value=human_duration(dur, lang), inline=True)
        e.add_field(name=_("stream.f_peak"), value=fmt_num(session.peak_viewers), inline=True)
        e.add_field(name=_("stream.f_avg"), value=fmt_num(session.avg_viewers), inline=True)
        e.add_field(name=_("stream.f_game"), value=session.game or "—", inline=True)
        e.add_field(name=_("stream.f_checkins"), value=str(session.checkins), inline=True)
        e.set_footer(text=f"{dot} {pname}")
        return e

    def _view(self, info_url: str, session_id: int | None, label_watch: str, label_checkin: str | None, ended: bool = False) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label=label_watch, url=info_url, emoji="📺"))
        if session_id and label_checkin:
            v.add_item(CheckinButton(session_id, label_checkin, disabled=ended))
        return v

    async def ensure_live_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """Kein Live-Channel eingestellt? Vorhandenen #stream-alerts/#live nehmen. Erstellt wird nie etwas – dafür gibt es /setup."""
        cfg = await config.get(guild.id, "streamer")
        ch = guild.get_channel(cfg.id("default_channel") or 0)
        if isinstance(ch, discord.TextChannel):
            return ch
        ch = next((c for c in guild.text_channels if ("stream-alert" in c.name.lower() or "live-alert" in c.name.lower() or c.name.lower().lstrip("🔴・-_ ").startswith("live"))), None)
        if ch is not None:
            data = dict(cfg)
            data["default_channel"] = str(ch.id)
            await config.save(guild.id, "streamer", settings=data)
        return ch

    async def _hub(self, hook: str, guild: discord.Guild, *args):
        """Streamer-Extras (streamhub.py, creator.py) – Fehler dort dürfen Live-Posts nie blockieren.
        Gibt das erste Ergebnis ungleich None zurück."""
        result = None
        for name in ("StreamHub", "Creator"):
            cog = self.bot.get_cog(name)
            fn = getattr(cog, hook, None) if cog else None
            if fn is None:
                continue
            try:
                value = await fn(guild, *args)
                result = value if result is None else result
            except Exception as exc:  # noqa: BLE001
                await capture_error(exc, guild_id=guild.id, command=f"{name}.{hook}")
        return result

    # ── Zustandswechsel ──
    async def go_live(self, guild: discord.Guild, s: Streamer, info: StreamInfo) -> None:
        cfg = await config.get(guild.id, "streamer")
        _ = await i18n.for_guild(guild.id)
        async with session_scope() as db:
            sess = StreamSession(guild_id=guild.id, streamer_id=s.id, platform_stream_id=info.stream_id, title=info.title[:300],
                                 game=info.game[:120], started_at=info.started_at or utcnow(), peak_viewers=info.viewers,
                                 viewer_sum=info.viewers, samples=1 if info.viewers else 0, checkins=0)
            db.add(sess)
            await db.flush()
            row = await db.get(Streamer, s.id)
            row.is_live, row.current_session_id = True, sess.id
            row.total_streams += 1
        metrics.incr(guild.id, "streams")
        channel = guild.get_channel(s.announce_channel_id or cfg.id("default_channel") or 0) or await self.ensure_live_channel(guild)
        msg = None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            role_id = s.ping_role_id or cfg.id("default_role")
            role = f"<@&{role_id}>" if role_id else ""
            template = s.message_template or cfg.get("message") or _("stream.msg_" + cfg.get("style", "hood"))
            content = fill(template, streamer=s.display_name, title=info.title, game=info.game, url=info.url, role=role,
                           platform=PLATFORM[s.platform][0])
            if role and "{role}" not in template:
                content = f"{role} {content}"
            view = self._view(info.url, sess.id, _("stream.watch"), _("stream.checkin") if cfg.get("checkin_enabled", True) else None)
            try:
                msg = await channel.send(content=content[:2000], embed=await self.live_embed(guild, s, info, sess), view=view,
                                         allowed_mentions=discord.AllowedMentions(roles=True, everyone=False))
            except discord.HTTPException as exc:
                log.warning("Live-Post fehlgeschlagen: %s", exc)
        async with session_scope() as db:
            row = await db.get(Streamer, s.id)
            row.live_message_id = msg.id if msg else None
            row.live_channel_id = msg.channel.id if msg else None
        await self._live_role(guild, s, True)
        await self._rename(guild, s, info, True)
        bus.publish(guild.id, "stream", {"action": "live", "streamer_id": s.id, "name": s.display_name, "title": info.title, "viewers": info.viewers})
        await log_event(guild.id, "stream", "live", content=f"{s.display_name}: {info.title}", details={"platform": s.platform, "session": sess.id})
        await self._hub("on_live", guild, s, info, sess)

    async def update_live(self, guild: discord.Guild, s: Streamer, info: StreamInfo) -> None:
        cfg = await config.get(guild.id, "streamer")
        async with session_scope() as db:
            sess = await db.get(StreamSession, s.current_session_id) if s.current_session_id else None
            old_game = sess.game if sess else ""
            if sess:
                sess.peak_viewers = max(sess.peak_viewers, info.viewers)
                sess.viewer_sum += info.viewers
                sess.samples += 1
                if info.title:
                    sess.title = info.title[:300]
                if info.game:
                    sess.game = info.game[:120]
        metrics.gauge(guild.id, "stream_viewers", info.viewers)
        if cfg.get("update_embed", True) and s.live_message_id and s.live_channel_id:
            ch = guild.get_channel(s.live_channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    await ch.get_partial_message(s.live_message_id).edit(embed=await self.live_embed(guild, s, info, sess))
                except discord.NotFound:
                    async with session_scope() as db:
                        (await db.get(Streamer, s.id)).live_message_id = None
                except discord.HTTPException:
                    pass
        bus.publish(guild.id, "stream", {"action": "update", "streamer_id": s.id, "viewers": info.viewers, "title": info.title})
        await self._hub("on_update", guild, s, info, old_game, sess)

    async def go_offline(self, guild: discord.Guild, s: Streamer) -> None:
        cfg = await config.get(guild.id, "streamer")
        _ = await i18n.for_guild(guild.id)
        async with session_scope() as db:
            sess = await db.get(StreamSession, s.current_session_id) if s.current_session_id else None
            if sess and not sess.ended_at:
                sess.ended_at = utcnow()
            row = await db.get(Streamer, s.id)
            row.is_live, row.current_session_id = False, None
            msg_id, ch_id = row.live_message_id, row.live_channel_id
            row.live_message_id = None
        if cfg.get("offline_edit", True) and sess and msg_id and ch_id:
            ch = guild.get_channel(ch_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                try:
                    url = {"twitch": f"https://twitch.tv/{s.channel}", "kick": f"https://kick.com/{s.channel}",
                           "youtube": f"https://youtube.com/channel/{s.platform_user_id or s.channel}"}[s.platform]
                    view = self._view(url, sess.id, _("stream.channel"), _("stream.checkin"), ended=True)
                    vod = await self._hub("vod_url", guild, s)
                    if vod:
                        view.add_item(discord.ui.Button(label=_("sh.vod"), url=vod, emoji="🎞️"))
                    await ch.get_partial_message(msg_id).edit(content=None, embed=await self.summary_embed(guild, s, sess), view=view)
                except discord.HTTPException:
                    pass
        await self._live_role(guild, s, False)
        await self._rename(guild, s, None, False)
        bus.publish(guild.id, "stream", {"action": "offline", "streamer_id": s.id})
        await log_event(guild.id, "stream", "offline", content=s.display_name, details={"session": sess.id if sess else None})
        await self._hub("on_offline", guild, s)

    async def _live_role(self, guild: discord.Guild, s: Streamer, live: bool) -> None:
        if not s.live_role_id or not s.discord_user_id:
            return
        member, role = guild.get_member(s.discord_user_id), guild.get_role(s.live_role_id)
        if not member or not role or role >= guild.me.top_role:
            return
        try:
            if live:
                await member.add_roles(role, reason="Stream live")
            else:
                await member.remove_roles(role, reason="Stream offline")
        except discord.HTTPException:
            pass

    async def _rename(self, guild: discord.Guild, s: Streamer, info: StreamInfo | None, live: bool) -> None:
        if not s.rename_channel_id:
            return
        ch = guild.get_channel(s.rename_channel_id)
        if ch is None:
            return
        name = fill(s.rename_live if live else s.rename_offline, streamer=s.display_name, title=(info.title if info else "")[:60],
                    game=info.game if info else "")[:95]
        if ch.name != name:
            try:
                await asyncio.wait_for(ch.edit(name=name, reason="Stream-Status"), timeout=10)
            except (discord.HTTPException, asyncio.TimeoutError):
                pass  # Discord erlaubt nur 2 Umbenennungen / 10 Min

    # ── Check-in ──
    async def checkin(self, interaction: discord.Interaction, session_id: int) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with session_scope() as db:
            sess = await db.get(StreamSession, session_id)
            if sess is None or sess.guild_id != interaction.guild_id or sess.ended_at:
                raise UserError("stream.checkin_closed")
            if await db.get(StreamCheckin, (session_id, interaction.user.id)):
                raise UserError("stream.already_checked_in")
            db.add(StreamCheckin(session_id=session_id, user_id=interaction.user.id))
            sess.checkins += 1
            streamer_row = await db.get(Streamer, sess.streamer_id)
            row = await ensure_member(db, interaction.guild_id, interaction.user)
            row.stream_checkins += 1
        lcfg = await config.get(interaction.guild_id, "levels")
        ecfg = await config.get(interaction.guild_id, "economy")
        rewards = []
        if lcfg.get("stream_xp") and await config.enabled(interaction.guild_id, "levels"):
            await progression.add_xp(self.bot, interaction.guild, interaction.user, int(lcfg["stream_xp"]), use_multiplier=False, announce=False)
            rewards.append(f"✨ {lcfg['stream_xp']} XP")
        if ecfg.get("stream_checkin") and await config.enabled(interaction.guild_id, "economy"):
            await progression.add_coins(interaction.guild_id, interaction.user, int(ecfg["stream_checkin"]), reason="stream")
            rewards.append(f"{ecfg.get('currency_emoji')} {ecfg['stream_checkin']}")
        await progression.quest_progress(self.bot, interaction.guild, interaction.user.id, "stream_checkins", 1)
        await achievements.check(self.bot, interaction.guild, row, interaction.user)
        metrics.incr(interaction.guild_id, "stream_checkins")
        await self._hub("on_checkin", interaction.guild, streamer_row, sess)
        new_role = await self._loyalty(interaction.guild, interaction.user, row.stream_checkins)  # type: ignore[arg-type]
        if new_role:
            rewards.append(f"🏅 {new_role.mention}")
        e = th.success(_("stream.checked_in_title"), _("stream.checked_in", rewards=" · ".join(rewards) or "—"))
        e.set_footer(text=_("stream.checkin_count", count=row.stream_checkins))
        await reply(interaction, e)

    async def _loyalty(self, guild: discord.Guild, member: discord.Member, checkins: int) -> discord.Role | None:
        """Stammzuschauer-Rollen: höchste erreichte Stufe vergeben, niedrigere entfernen."""
        cfg = await config.get(guild.id, "streamer")
        tiers = sorted([t for t in cfg.get("loyalty_roles") or [] if t.get("role")], key=lambda t: t["checkins"])
        reached = [t for t in tiers if checkins >= t["checkins"]]
        if not reached:
            return None
        target = guild.get_role(int(reached[-1]["role"]))
        if target is None or target >= guild.me.top_role:
            return None
        lower = [guild.get_role(int(t["role"])) for t in reached[:-1]]
        try:
            await member.remove_roles(*[r for r in lower if r and r in member.roles and r < guild.me.top_role], reason="Stammzuschauer-Stufe")
            if target not in member.roles:
                await member.add_roles(target, reason=f"Stammzuschauer: {checkins} Check-ins")
                return target
        except discord.HTTPException:
            pass
        return None

    # ── Clips ──
    async def _clips(self, guild: discord.Guild, rows: list[Streamer], creds: dict) -> None:
        cfg = await config.get(guild.id, "streamer")
        channel = guild.get_channel(cfg.id("clips_channel") or 0)
        if not cfg.get("clips_enabled", True) or not isinstance(channel, discord.TextChannel):
            return
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        for s in rows:
            if not s.platform_user_id:
                continue
            since = s.last_clip_at or utcnow()
            try:
                clips = await twitch.clips(creds, s.platform_user_id, since)
            except PlatformError:
                continue
            newest = since
            for c in sorted(clips, key=lambda c: c["created_at"]):
                created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
                if created <= since or c.get("view_count", 0) < cfg.get("clips_min_views", 0):
                    newest = max(newest, created)
                    continue
                newest = max(newest, created)
                e = discord.Embed(title=c["title"][:256], url=c["url"], colour=th.color("primary"), timestamp=created)
                e.set_author(name=_("stream.clip_author", streamer=s.display_name), icon_url=s.avatar_url)
                e.set_image(url=c.get("thumbnail_url"))
                e.add_field(name=_("stream.clip_by"), value=c.get("creator_name", "—"), inline=True)
                e.add_field(name=_("stream.clip_views"), value=fmt_num(c.get("view_count", 0)), inline=True)
                e.add_field(name=_("stream.clip_length"), value=f"{c.get('duration', 0):.0f}s", inline=True)
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label=_("stream.clip_watch"), url=c["url"], emoji="🎬"))
                try:
                    await channel.send(embed=e, view=view)
                except discord.HTTPException:
                    pass
            async with session_scope() as db:
                row = await db.get(Streamer, s.id)
                row.last_clip_at = newest if s.last_clip_at else utcnow()

    # ── Polling ──
    @tasks.loop(seconds=90)
    async def poll_loop(self):
        guild_ids = [g.id for g in self.bot.guilds]
        async with SessionLocal() as db:
            rows = (await db.execute(select(Streamer).where(Streamer.enabled.is_(True), Streamer.guild_id.in_(guild_ids)))).scalars().all()
        by_guild: dict[int, list[Streamer]] = {}
        for s in rows:
            by_guild.setdefault(s.guild_id, []).append(s)
        for gid, streamers in by_guild.items():
            if not await config.enabled(gid, "streamer"):
                continue
            guild = self.bot.get_guild(gid)
            try:
                await self.poll_guild(guild, streamers)
            except Exception as exc:  # noqa: BLE001
                await capture_error(exc, guild_id=gid, command="streamer.poll")

    @poll_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def poll_guild(self, guild: discord.Guild, streamers: list[Streamer]) -> None:
        twitch_rows = [s for s in streamers if s.platform == "twitch"]
        if twitch_rows:
            try:
                creds = await get_credentials(guild.id, "twitch")
                infos = await twitch.streams(creds, [s.channel for s in twitch_rows])
                missing = [s for s in twitch_rows if not s.platform_user_id]
                if missing:
                    users = await twitch.users(creds, [s.channel for s in missing])
                    await self._store_channel_info(missing, users)
                for s in twitch_rows:
                    await self._apply(guild, s, infos.get(s.channel.lower(), StreamInfo(False)))
                await self._hourly_stats(guild, twitch_rows, creds)
                await self._clips(guild, twitch_rows, creds)
            except PlatformError as exc:
                await self._error(twitch_rows, str(exc))
        for s in streamers:
            if s.platform == "youtube":
                try:
                    await self._poll_youtube(guild, s)
                except PlatformError as exc:
                    await self._error([s], str(exc))
            elif s.platform == "kick":
                try:
                    info_ch, info = await kick.channel(await get_credentials(guild.id, "kick"), s.channel)
                    if not s.platform_user_id or s.avatar_url != info_ch.avatar:
                        await self._store_channel_info([s], {s.channel.lower(): info_ch})
                    await self._apply(guild, s, info)
                except PlatformError as exc:
                    await self._error([s], str(exc))

    async def _store_channel_info(self, rows: list[Streamer], infos: dict) -> None:
        async with session_scope() as db:
            for s in rows:
                ci = infos.get(s.channel.lower())
                if ci:
                    row = await db.get(Streamer, s.id)
                    row.platform_user_id, row.display_name, row.avatar_url = ci.user_id, ci.display_name, ci.avatar
                    s.platform_user_id, s.display_name, s.avatar_url = ci.user_id, ci.display_name, ci.avatar

    async def _error(self, rows: list[Streamer], msg: str) -> None:
        async with session_scope() as db:
            for s in rows:
                row = await db.get(Streamer, s.id)
                if row:
                    row.last_error, row.last_checked = msg[:300], utcnow()

    async def _apply(self, guild: discord.Guild, s: Streamer, info: StreamInfo) -> None:
        async with session_scope() as db:
            row = await db.get(Streamer, s.id)
            row.last_checked, row.last_error = utcnow(), None
            if info.live:
                db.add(StreamSnapshot(streamer_id=s.id, live=True, viewers=info.viewers))
        if info.live and not s.is_live:
            await self.go_live(guild, s, info)
        elif info.live and s.is_live:
            async with SessionLocal() as db:
                sess = await db.get(StreamSession, s.current_session_id) if s.current_session_id else None
            if sess and info.stream_id and sess.platform_stream_id and sess.platform_stream_id != info.stream_id:
                await self.go_offline(guild, s)
                async with SessionLocal() as db:
                    s = await db.get(Streamer, s.id)
                await self.go_live(guild, s, info)
            else:
                await self.update_live(guild, s, info)
        elif not info.live and s.is_live:
            await self.go_offline(guild, s)

    async def _hourly_stats(self, guild: discord.Guild, rows: list[Streamer], creds: dict) -> None:
        now = utcnow().timestamp()
        for s in rows:
            if now - self._last_stats.get(s.id, 0) < 3600:
                continue
            self._last_stats[s.id] = now
            followers = await twitch.followers(creds, s.platform_user_id) if s.platform_user_id else None
            await self._snapshot_stats(s, followers, None)

    async def _snapshot_stats(self, s: Streamer, followers: int | None, subscribers: int | None) -> None:
        async with session_scope() as db:
            row = await db.get(Streamer, s.id)
            if followers is not None:
                row.followers = followers
            if subscribers is not None:
                row.subscribers = subscribers
            db.add(StreamSnapshot(streamer_id=s.id, live=row.is_live, viewers=0, followers=row.followers, subscribers=row.subscribers))

    async def _poll_youtube(self, guild: discord.Guild, s: Streamer) -> None:
        creds = await get_credentials(guild.id, "youtube")
        key = creds.get("api_key", "")
        if not s.platform_user_id or utcnow().timestamp() - self._last_stats.get(s.id, 0) >= 3600:
            ch = await youtube.channel(key, s.channel)
            self._last_stats[s.id] = utcnow().timestamp()
            await self._store_channel_info([s], {s.channel.lower(): ch})
            await self._snapshot_stats(s, None, ch.subscribers)
        ids = [i for i in await youtube.recent_video_ids(s.platform_user_id, 5) if i]
        videos = await youtube.videos(key, ids)
        seen = list(s.seen_video_ids or [])
        first_run = not seen
        live_video = next((v for v in videos if v.kind == "live" and not v.ended), None)
        for v in videos:
            if v.video_id in seen or v.kind in ("live", "upcoming"):
                continue
            seen.insert(0, v.video_id)
            recent = v.published and utcnow() - v.published < timedelta(days=2)
            if first_run or not recent:
                continue
            if (v.kind == "short" and s.notify_shorts) or (v.kind == "video" and s.notify_videos):
                await self._announce_video(guild, s, v)
        if live_video and live_video.video_id not in seen:
            seen.insert(0, live_video.video_id)
        async with session_scope() as db:
            row = await db.get(Streamer, s.id)
            row.seen_video_ids = seen[:50]
            row.last_video_id = ids[0] if ids else row.last_video_id
        info = StreamInfo(bool(live_video), live_video.video_id if live_video else None, live_video.title if live_video else "", "YouTube",
                          live_video.viewers if live_video else 0, live_video.started_at if live_video else None,
                          live_video.thumbnail if live_video else None, live_video.url if live_video else "")
        await self._apply(guild, s, info)

    async def _announce_video(self, guild: discord.Guild, s: Streamer, v) -> None:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = discord.Embed(title=v.title[:256], url=v.url, colour=hex_to_color("#ff0033"), timestamp=v.published or utcnow())
        e.set_author(name=s.display_name, icon_url=s.avatar_url)
        if v.thumbnail:
            e.set_image(url=v.thumbnail)
        e.set_footer(text=("📱 Short" if v.kind == "short" else "📺 Video") + f" · {th.footer}", icon_url=th.icon_url)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=_("stream.watch"), url=v.url, emoji="▶️"))
        msg = await notify(guild, v.kind, embed=e, view=view, streamer=s.display_name, title=v.title, url=v.url, server=guild.name)
        if msg is None and s.announce_channel_id:
            ch = guild.get_channel(s.announce_channel_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(content=_("stream.new_" + v.kind, streamer=s.display_name), embed=e, view=view)
                except discord.HTTPException:
                    pass
        await log_event(guild.id, "stream", v.kind, content=f"{s.display_name}: {v.title}", details={"url": v.url})

    async def check_now(self, guild: discord.Guild, streamer_id: int) -> Streamer:
        async with SessionLocal() as db:
            s = await db.get(Streamer, streamer_id)
        if s is None or s.guild_id != guild.id:
            raise UserError("stream.not_found")
        await self.poll_guild(guild, [s])
        async with SessionLocal() as db:
            return await db.get(Streamer, streamer_id)

    @app_commands.command(name="streams", description="Zeigt alle verbundenen Streamer und ihren Live-Status")
    @app_commands.guild_only()
    async def streams(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as db:
            rows = (await db.execute(select(Streamer).where(Streamer.guild_id == interaction.guild_id, Streamer.enabled.is_(True)))).scalars().all()
        if not rows:
            await reply(interaction, th.info(_("stream.list_title"), _("stream.list_empty")))
            return
        lines = []
        for s in rows:
            pname, _c, dot = PLATFORM[s.platform]
            url = {"twitch": f"https://twitch.tv/{s.channel}", "kick": f"https://kick.com/{s.channel}",
                   "youtube": f"https://youtube.com/channel/{s.platform_user_id or s.channel}"}[s.platform]
            status = "🔴 **LIVE**" if s.is_live else "⚫ Offline"
            lines.append(f"{dot} [{s.display_name or s.channel}]({url}) · {pname} · {status}")
        await reply(interaction, th.embed(_("stream.list_title"), "\n".join(lines), icon=False), ephemeral=False)


    # ── Manuelle Ankündigung, Plan, Rangliste ──
    async def _streamer_choices(self, interaction: discord.Interaction, current: str):
        async with SessionLocal() as db:
            rows = (await db.execute(select(Streamer).where(Streamer.guild_id == interaction.guild_id))).scalars().all()
        return [app_commands.Choice(name=f"{PLATFORM[s.platform][2]} {s.display_name or s.channel}"[:100], value=s.id)
                for s in rows if current.lower() in (s.display_name or s.channel).lower()][:25]

    @app_commands.command(name="golive", description="Stream manuell ankündigen (z. B. wenn die API noch nicht verbunden ist)")
    @app_commands.guild_only()
    @app_commands.describe(link="Link zum Stream", title="Worum geht's im Stream?", ping="Stream-Rolle pingen")
    async def golive(self, interaction: discord.Interaction, link: str, title: app_commands.Range[str, 0, 200] = "", ping: bool = True):
        from app.services.notify import is_admin
        cfg = await config.get(interaction.guild_id, "streamer")
        member: discord.Member = interaction.user  # type: ignore[assignment]
        allowed = set(cfg.ids("golive_roles"))
        if not await is_admin(member) and not ({r.id for r in member.roles} & allowed):
            raise UserError("errors.no_permission")
        if not link.startswith(("https://", "http://")):
            raise UserError("stream.golive_bad_link")
        channel = interaction.guild.get_channel(cfg.id("default_channel") or 0) or interaction.channel
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        name = member.display_name
        lines = _("stream.hood_lines").split("|")
        e = discord.Embed(title=_("stream.hood_title", streamer=name.upper()), url=link,
                          description=fill(lines[member.id % len(lines)], streamer=name) + (f"\n\n**{title}**" if title else ""),
                          colour=th.color("primary"), timestamp=utcnow())
        e.set_author(name=name, icon_url=member.display_avatar.url)
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=th.footer, icon_url=th.icon_url)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=_("stream.watch"), url=link, emoji="📺"))
        role_id = cfg.id("default_role")
        content = f"<@&{role_id}>" if ping and role_id else None
        await channel.send(content=content, embed=e, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
        await log_event(interaction.guild_id, "stream", "golive", user=member, content=link)
        await reply(interaction, th.success(_("common.success"), _("stream.golive_done", channel=channel.mention)))

    @app_commands.command(name="schedule", description="Der Stream-Plan (Twitch)")
    @app_commands.guild_only()
    async def schedule(self, interaction: discord.Interaction, streamer: int | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as db:
            q = select(Streamer).where(Streamer.guild_id == interaction.guild_id, Streamer.platform == "twitch")
            if streamer:
                q = q.where(Streamer.id == streamer)
            s = (await db.execute(q.limit(1))).scalar_one_or_none()
        if s is None or not s.platform_user_id:
            raise UserError("stream.schedule_none")
        await interaction.response.defer()
        try:
            segs = await twitch.schedule(await get_credentials(interaction.guild_id, "twitch"), s.platform_user_id)
        except PlatformError:
            raise UserError("stream.schedule_failed")
        if not segs:
            await interaction.followup.send(embed=th.info(_("stream.schedule_title", streamer=s.display_name), _("stream.schedule_empty")))
            return
        lines = []
        for seg in segs[:7]:
            start = datetime.fromisoformat(seg["start_time"].replace("Z", "+00:00"))
            game = (seg.get("category") or {}).get("name") or ""
            lines.append(f"**{ts(start, 'F')}** · {ts(start, 'R')}\n╰ {seg.get('title') or '—'}{f' · {game}' if game else ''}")
        e = th.embed(_("stream.schedule_title", streamer=s.display_name), "\n\n".join(lines), icon=False)
        if s.avatar_url:
            e.set_thumbnail(url=s.avatar_url)
        await interaction.followup.send(embed=e)

    @schedule.autocomplete("streamer")
    async def _schedule_ac(self, interaction: discord.Interaction, current: str):
        return await self._streamer_choices(interaction, current)

    @app_commands.command(name="streamtop", description="Die loyalsten Zuschauer – meiste Stream-Check-ins")
    @app_commands.guild_only()
    async def streamtop(self, interaction: discord.Interaction):
        from app.db.models import Member
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as db:
            rows = (await db.execute(select(Member).where(Member.guild_id == interaction.guild_id, Member.stream_checkins > 0, Member.in_guild.is_(True))
                                     .order_by(Member.stream_checkins.desc()).limit(15))).scalars().all()
        if not rows:
            await reply(interaction, th.info(_("stream.top_title"), _("stream.top_empty")), ephemeral=False)
            return
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"{medals.get(i, f'`#{i:>2}`')} <@{r.user_id}> — **{r.stream_checkins}** Streams" for i, r in enumerate(rows, 1)]
        await reply(interaction, th.embed(_("stream.top_title"), "\n".join(lines), icon=False), ephemeral=False)


    # ── Medias: „Mit Twitch verbinden“ ──
    twitch_group = app_commands.Group(name="twitch", description="Deinen Twitch-Kanal verbinden (nur Medias)", guild_only=True)

    async def connect_flow(self, interaction: discord.Interaction) -> None:
        """Schritt 1: Media-Check, dann Formular für den Twitch-Namen."""
        _ = await i18n.for_guild(interaction.guild_id)
        cfg = await config.get(interaction.guild_id, "streamer")
        member: discord.Member = interaction.user  # type: ignore[assignment]
        role_id = cfg.id("media_role")
        if role_id and not member.get_role(role_id) and not member.guild_permissions.manage_guild:
            raise UserError("stream.media_only", role=f"<@&{role_id}>")
        await interaction.response.send_modal(TwitchNameModal(self, _))

    async def verify_flow(self, interaction: discord.Interaction, name: str) -> None:
        """Schritt 2: Kanal prüfen, Code zeigen, im Twitch-Chat auf den Code warten."""
        from app.services import twitch_chat, twitch_link
        await interaction.response.defer(ephemeral=True, thinking=True)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        login = re.sub(r"^(https?://)?(www\.|m\.)?twitch\.tv/", "", name.strip(), flags=re.I).strip("/@ ").split("/")[0].lower()
        if not re.fullmatch(r"[a-z0-9_]{3,25}", login):
            raise UserError("stream.verify_bad_name")
        creds = await get_credentials(interaction.guild_id, "twitch")
        try:
            found = await twitch.users(creds, [login])
        except PlatformError:
            raise UserError("stream.verify_no_api") from None
        info = found.get(login)
        if info is None:
            raise UserError("stream.verify_not_found", name=login)
        key = (interaction.guild_id, interaction.user.id)
        if (old := self._verifying.pop(key, None)) is not None:
            old.cancel()
        code = twitch_chat.new_code()
        until = utcnow() + timedelta(seconds=VERIFY_SECONDS)
        e = th.embed(_("stream.verify_title"), _("stream.verify_steps", name=info.display_name, code=code, until=ts(until, "R")), icon=False)
        e.add_field(name=_("stream.verify_tips_title"), value=_("stream.verify_tips", name=info.display_name), inline=False)
        if info.avatar:
            e.set_thumbnail(url=info.avatar)
        view = discord.ui.View(timeout=VERIFY_SECONDS)
        view.add_item(discord.ui.Button(label=_("stream.verify_open_chat"), emoji="💬", url=f"https://www.twitch.tv/popout/{login}/chat"))
        await interaction.followup.send(embed=e, view=view, ephemeral=True)

        async def waiter() -> None:
            try:
                ok = await twitch_chat.wait_for_code(login, info.user_id, code, VERIFY_SECONDS)
                if not ok:
                    await interaction.edit_original_response(embed=th.error(_("stream.verify_timeout_title"), _("stream.verify_timeout")), view=None)
                    return
                tw = {"id": info.user_id, "login": login, "display_name": info.display_name, "profile_image_url": info.avatar}
                s = await twitch_link.link_streamer(interaction.guild_id, interaction.user.id, str(interaction.user), tw)
                await self._hub("on_connect", interaction.guild, interaction.user, s)
                live_ch = interaction.guild.get_channel(s.announce_channel_id or 0) or await self.ensure_live_channel(interaction.guild)
                done = th.success(_("stream.verify_done_title"), _("stream.verify_done", channel=s.channel))
                if live_ch:
                    done.add_field(name=_("stream.verify_where"), value=live_ch.mention, inline=False)
                if info.avatar:
                    done.set_thumbnail(url=info.avatar)
                await interaction.edit_original_response(embed=done, view=None)
                try:
                    await interaction.user.send(embed=done)
                except discord.HTTPException:
                    pass
            except asyncio.CancelledError:
                raise
            except discord.HTTPException:
                pass
            except Exception as exc:  # noqa: BLE001
                await capture_error(exc, guild_id=interaction.guild_id, user_id=interaction.user.id, command="twitch_verify")
            finally:
                if self._verifying.get(key) is asyncio.current_task():
                    self._verifying.pop(key, None)

        self._verifying[key] = asyncio.create_task(waiter())

    async def send_panel(self, guild: discord.Guild, channel: discord.TextChannel | None = None) -> discord.Message:
        """Media-Panel mit „Mit Twitch verbinden“-Button."""
        cfg = await config.get(guild.id, "streamer")
        channel = channel or guild.get_channel(cfg.id("media_panel_channel") or 0)
        if not isinstance(channel, discord.TextChannel):
            raise UserError("stream.no_media_channel")
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = th.embed(_("stream.media_panel_title"), _("stream.media_panel_text"), icon=False)
        if cfg.id("media_role"):
            e.add_field(name=_("stream.media_for"), value=f"<@&{cfg.id('media_role')}>", inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(TwitchConnectButton(_("stream.connect_button")))
        return await channel.send(embed=e, view=view, allowed_mentions=discord.AllowedMentions.none())

    @twitch_group.command(name="connect", description="Verbinde deinen Twitch-Kanal – deine Streams werden automatisch angekündigt")
    async def twitch_connect(self, interaction: discord.Interaction):
        await self.connect_flow(interaction)

    @twitch_group.command(name="disconnect", description="Twitch-Verbindung trennen")
    async def twitch_disconnect(self, interaction: discord.Interaction):
        from app.services import twitch_link
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        if not await twitch_link.unlink(interaction.guild_id, interaction.user.id):
            raise UserError("stream.not_connected")
        await reply(interaction, th.success(_("common.success"), _("stream.disconnected")))

    @twitch_group.command(name="panel", description="Postet das „Mit Twitch verbinden“-Panel für Medias (Admin)")
    async def twitch_panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        from app.services.notify import is_admin
        if not await is_admin(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        target = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        msg = await self.send_panel(interaction.guild, target)  # type: ignore[arg-type]
        cfg = dict(await config.get(interaction.guild_id, "streamer"))
        cfg["media_panel_channel"] = str(target.id)
        await config.save(interaction.guild_id, "streamer", settings=cfg)
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(_("common.success"), _("tickets.panel_sent_in", channel=target.mention, url=msg.jump_url)), ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(CheckinButton, TwitchConnectButton)
    await bot.add_cog(StreamerCog(bot))
