"""Streamer-Extras: Live-Board, Stream-Alarme per DM, Stream-Wetten mit Coins, Discord-Event bei Live, Spielwechsel,
Zuschauer-Rekorde, Meilensteine & Streaks, Hype-Meldungen, VOD-Button, Erinnerung vor geplanten Streams,
Wochenplan, Clip der Woche, Shoutouts und Stream-Statistiken.

Der StreamerCog ruft die `on_*`-Hooks auf; alles Zeitgesteuerte läuft im `tick`-Loop (1× pro Minute)."""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from app.bot.ui import handle_exception, reply
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import capture_error, log_event
from app.core.timeutil import human_duration, local_now, tz, ts, week_key
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Member, Prediction, PredictionBet, Streamer, StreamAlert, StreamSession
from app.services.integrations import get_credentials
from app.services.members import ensure_member
from app.services.progression import add_coins
from app.services.streaming import PlatformError, StreamInfo, twitch

log = logging.getLogger("nova.streamhub")
STREAM_MILESTONES = {10, 25, 50, 100, 150, 200, 250, 365, 500, 750, 1000}
STREAK_MILESTONES = {3, 5, 7, 10, 14, 21, 30, 50, 75, 100}
WEEKDAYS = [("0", "Montag"), ("1", "Dienstag"), ("2", "Mittwoch"), ("3", "Donnerstag"), ("4", "Freitag"), ("5", "Samstag"), ("6", "Sonntag")]


def stream_url(s: Streamer) -> str:
    return {"twitch": f"https://twitch.tv/{s.channel}", "kick": f"https://kick.com/{s.channel}",
            "youtube": f"https://youtube.com/channel/{s.platform_user_id or s.channel}"}[s.platform]


def streak_days(days: set[date], today: date) -> int:
    """Wie viele Tage am Stück (bis heute oder gestern) gestreamt wurde."""
    day = today if today in days else today - timedelta(days=1)
    n = 0
    while day in days:
        n += 1
        day -= timedelta(days=1)
    return n


def split_pot(bets: list[tuple[int, int, int]], winner: int) -> dict[int, int]:
    """bets = [(user_id, option, amount)] → Auszahlung je User. Gewinner bekommen Einsatz + Anteil am Verlierer-Pot.
    Hat niemand richtig getippt, bekommt jeder seinen Einsatz zurück."""
    win = [(u, a) for u, o, a in bets if o == winner]
    if not win:
        return {u: a for u, _o, a in bets}
    win_pool = sum(a for _u, a in win)
    lose_pool = sum(a for _u, o, a in bets if o != winner)
    return {u: a + (a * lose_pool) // win_pool for u, a in win}


async def can_host(member: discord.Member) -> bool:
    """Admins oder Rollen aus „Wer darf /golive nutzen“."""
    from app.services.notify import is_admin
    if await is_admin(member):
        return True
    cfg = await config.get(member.guild.id, "streamer")
    return bool({r.id for r in member.roles} & set(cfg.ids("golive_roles")))


# ───────────────────────── Wetten: Buttons & Formular ─────────────────────────
class BetButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:bet:(?P<pid>\d+):(?P<opt>\d+)"):
    def __init__(self, pid: int, opt: int, label: str | None = None, disabled: bool = False):
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary if opt else discord.ButtonStyle.primary,
                                           custom_id=f"nova:bet:{pid}:{opt}", disabled=disabled))
        self.pid, self.opt = pid, opt

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["pid"]), int(match["opt"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            _ = await i18n.for_guild(interaction.guild_id)
            async with SessionLocal() as db:
                p = await db.get(Prediction, self.pid)
            if p is None or p.status != "open" or p.closes_at <= utcnow():
                raise UserError("sh.bet_closed")
            await interaction.response.send_modal(BetModal(self.pid, self.opt, p.options[self.opt], _))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "bet")


class BetModal(discord.ui.Modal):
    def __init__(self, pid: int, opt: int, option: str, _):
        super().__init__(title=_("sh.bet_modal_title", option=option)[:45])
        self.pid, self.opt = pid, opt
        self.amount = discord.ui.TextInput(label=_("sh.bet_modal_label")[:45], placeholder=_("sh.bet_modal_placeholder")[:100], max_length=12)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hub: StreamHub = interaction.client.get_cog("StreamHub")  # type: ignore[assignment]
            await hub.place_bet(interaction, self.pid, self.opt, self.amount.value)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "bet")


# ───────────────────────── Cog ─────────────────────────
class StreamHub(commands.Cog, name="StreamHub"):
    module = "streamer"
    help_category = "streamer"

    stream = app_commands.Group(name="stream", description="Stream-Infos, Rangliste, Shoutouts", guild_only=True)
    alarm = app_commands.Group(name="streamalarm", description="Per DM benachrichtigt werden, wenn ein Streamer live geht", guild_only=True)
    bet = app_commands.Group(name="wette", description="Stream-Wetten mit Coins", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._viewers: dict[int, int] = {}            # streamer_id → aktuelle Zuschauer
        self._board_at: dict[int, float] = {}         # guild_id → letztes Live-Board-Update
        self._record_done: set[int] = set()           # session_ids mit bereits gemeldetem Rekord
        self._reminded: set[str] = set()              # Twitch-Plan-Segmente, an die schon erinnert wurde
        self._plans: dict[int, tuple[float, list]] = {}  # streamer_id → (Ablauf, Segmente)
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    async def _ctx(self, guild_id: int):
        return await i18n.for_guild(guild_id), await config.get(guild_id, "streamer")

    @staticmethod
    def _text_channel(guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
        ch = guild.get_channel(channel_id or 0)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _live_channel(self, guild: discord.Guild, s: Streamer) -> discord.TextChannel | None:
        cfg = await config.get(guild.id, "streamer")
        return self._text_channel(guild, s.live_channel_id or s.announce_channel_id or cfg.id("default_channel"))

    async def _say(self, guild: discord.Guild, s: Streamer, embed: discord.Embed) -> None:
        """Kurze Zusatzmeldung – als Antwort auf den Live-Post, falls vorhanden."""
        ch = await self._live_channel(guild, s)
        if ch is None:
            return
        ref = ch.get_partial_message(s.live_message_id) if s.live_message_id and s.live_channel_id == ch.id else None
        try:
            await ch.send(embed=embed, reference=ref, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            try:
                await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass

    # ── Hooks aus dem StreamerCog ──
    async def on_live(self, guild: discord.Guild, s: Streamer, info: StreamInfo, sess: StreamSession) -> None:
        _, cfg = await self._ctx(guild.id)
        async with SessionLocal() as db:
            s = await db.get(Streamer, s.id)
        self._viewers[s.id] = info.viewers
        await self._alarms(guild, s, info)
        if cfg.get("live_event", True):
            await self._start_event(guild, s, info)
        if cfg.get("records", True):
            await self._milestones(guild, s)
        await self.refresh_board(guild, force=True)

    async def on_update(self, guild: discord.Guild, s: Streamer, info: StreamInfo, old_game: str, sess: StreamSession | None) -> None:
        _, cfg = await self._ctx(guild.id)
        self._viewers[s.id] = info.viewers
        th = await theme(guild)
        if cfg.get("game_change", True) and old_game and info.game and old_game != info.game:
            e = discord.Embed(description=_("sh.game_change", streamer=s.display_name, old=old_game, new=info.game, url=info.url),
                              colour=th.color("primary"))
            await self._say(guild, s, e)
        if cfg.get("records", True) and sess and sess.id not in self._record_done:
            async with SessionLocal() as db:
                best = (await db.execute(select(func.max(StreamSession.peak_viewers)).where(
                    StreamSession.streamer_id == s.id, StreamSession.id != sess.id))).scalar() or 0
            if best >= 5 and sess.peak_viewers > best:
                self._record_done.add(sess.id)
                e = th.success(_("sh.record_title"), _("sh.record_text", streamer=s.display_name, viewers=fmt_num(sess.peak_viewers), old=fmt_num(best)))
                await self._say(guild, s, e)
        await self.refresh_board(guild)

    async def on_offline(self, guild: discord.Guild, s: Streamer) -> None:
        self._viewers.pop(s.id, None)
        await self._end_event(guild, s)
        await self.refresh_board(guild, force=True)

    async def on_checkin(self, guild: discord.Guild, s: Streamer | None, sess: StreamSession) -> None:
        _, cfg = await self._ctx(guild.id)
        levels = {int(x) for x in cfg.get("hype_levels") or [] if str(x).isdigit()}
        if s is None or sess.checkins not in levels:
            return
        th = await theme(guild)
        e = discord.Embed(title=_("sh.hype_title", count=sess.checkins), description=_("sh.hype_text", streamer=s.display_name, count=sess.checkins),
                          colour=th.color("primary"))
        await self._say(guild, s, e)

    async def vod_url(self, guild: discord.Guild, s: Streamer) -> str | None:
        cfg = await config.get(guild.id, "streamer")
        if s.platform != "twitch" or not s.platform_user_id or not cfg.get("vod_button", True):
            return None
        try:
            vod = await twitch.latest_vod(await get_credentials(guild.id, "twitch"), s.platform_user_id)
        except PlatformError:
            return None
        return vod.get("url") if vod else None

    # ── Stream-Alarme ──
    async def _alarms(self, guild: discord.Guild, s: Streamer, info: StreamInfo) -> None:
        async with SessionLocal() as db:
            users = (await db.execute(select(StreamAlert.user_id).where(StreamAlert.streamer_id == s.id))).scalars().all()
        if not users:
            return
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = discord.Embed(title=_("sh.alarm_dm_title", streamer=s.display_name), url=info.url,
                          description=f"**{info.title}**\n{info.game}" if info.title else info.game or None, colour=th.color("primary"))
        if info.thumbnail:
            e.set_image(url=info.thumbnail)
        if s.avatar_url:
            e.set_thumbnail(url=s.avatar_url)
        e.set_footer(text=_("sh.alarm_dm_footer", server=guild.name))
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=_("stream.watch"), url=info.url, emoji="📺"))
        for uid in users:
            member = guild.get_member(uid)
            if member:
                try:
                    await member.send(embed=e, view=view)
                except discord.HTTPException:
                    pass

    # ── Discord-Event „X ist live“ ──
    async def _start_event(self, guild: discord.Guild, s: Streamer, info: StreamInfo) -> None:
        if not guild.me.guild_permissions.manage_events:
            return
        _ = await i18n.for_guild(guild.id)
        now = utcnow()
        try:
            ev = await guild.create_scheduled_event(
                name=_("sh.event_name", streamer=s.display_name)[:100], description=(info.title or "")[:1000],
                start_time=now + timedelta(seconds=45), end_time=now + timedelta(hours=12),
                entity_type=discord.EntityType.external, location=info.url[:100], privacy_level=discord.PrivacyLevel.guild_only)
        except discord.HTTPException as exc:
            log.info("Live-Event nicht erstellt: %s", exc)
            return
        await config.set_state(f"sh:ev:{s.id}", ev.id)

    async def _end_event(self, guild: discord.Guild, s: Streamer) -> None:
        ev_id = await config.state(f"sh:ev:{s.id}")
        if not ev_id:
            return
        await config.set_state(f"sh:ev:{s.id}", None)
        ev = guild.get_scheduled_event(int(ev_id))
        if ev is None:
            return
        try:
            if ev.status == discord.EventStatus.active:
                await ev.end()
            else:
                await ev.delete()
        except discord.HTTPException:
            pass

    async def _activate_events(self, guild: discord.Guild) -> None:
        for ev in guild.scheduled_events:
            if ev.creator_id == guild.me.id and ev.status == discord.EventStatus.scheduled and ev.start_time <= utcnow():
                try:
                    await ev.start()
                except discord.HTTPException:
                    pass

    # ── Meilensteine & Streak ──
    async def _milestones(self, guild: discord.Guild, s: Streamer) -> None:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        lines = []
        if s.total_streams in STREAM_MILESTONES:
            lines.append(_("sh.milestone_streams", streamer=s.display_name, count=s.total_streams))
        streak = await self.streak(guild.id, s.id)
        if streak in STREAK_MILESTONES:
            lines.append(_("sh.milestone_streak", streamer=s.display_name, days=streak))
        if lines:
            await self._say(guild, s, th.success(_("sh.milestone_title"), "\n".join(lines)))

    async def streak(self, guild_id: int, streamer_id: int) -> int:
        gcfg = await config.get(guild_id, "general")
        zone = tz(gcfg.get("timezone"))
        since = utcnow() - timedelta(days=120)
        async with SessionLocal() as db:
            starts = (await db.execute(select(StreamSession.started_at).where(
                StreamSession.streamer_id == streamer_id, StreamSession.started_at >= since))).scalars().all()
        days = {d.astimezone(zone).date() for d in starts}
        return streak_days(days, local_now(gcfg.get("timezone")).date())

    # ── Live-Board ──
    async def refresh_board(self, guild: discord.Guild, force: bool = False) -> None:
        _, cfg = await self._ctx(guild.id)
        ch = self._text_channel(guild, cfg.id("live_board_channel"))
        if ch is None:
            return
        if not force and time.monotonic() - self._board_at.get(guild.id, 0) < 120:
            return
        self._board_at[guild.id] = time.monotonic()
        th = await theme(guild)
        async with SessionLocal() as db:
            live = (await db.execute(select(Streamer).where(Streamer.guild_id == guild.id, Streamer.is_live.is_(True)))).scalars().all()
            sessions = {x.id: x for x in (await db.execute(select(StreamSession).where(
                StreamSession.id.in_([s.current_session_id for s in live if s.current_session_id])))).scalars().all()}
        if live:
            rows = []
            for s in sorted(live, key=lambda x: -self._viewers.get(x.id, 0))[:20]:
                sess = sessions.get(s.current_session_id or 0)
                bits = [f"**[{s.display_name}]({stream_url(s)})**"]
                if sess and sess.game:
                    bits.append(sess.game)
                bits.append(f"👀 {fmt_num(self._viewers.get(s.id, sess.peak_viewers if sess else 0))}")
                if sess:
                    bits.append(ts(sess.started_at, "R"))
                rows.append("🔴 " + " · ".join(bits))
            e = discord.Embed(title=_("sh.board_title", count=len(live)), description="\n".join(rows), colour=th.color("primary"), timestamp=utcnow())
        else:
            e = discord.Embed(title=_("sh.board_title_none"), description=_("sh.board_empty"), colour=discord.Colour.dark_grey(), timestamp=utcnow())
        e.set_footer(text=_("sh.board_footer"))
        key = f"sh:board:{guild.id}"
        msg_id = await config.state(key)
        if msg_id:
            try:
                await ch.get_partial_message(int(msg_id)).edit(embed=e)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException:
                return
        try:
            msg = await ch.send(embed=e)
            await config.set_state(key, msg.id)
        except discord.HTTPException:
            pass

    # ── Zeitgesteuert ──
    @tasks.loop(minutes=1)
    async def tick(self):
        for guild in list(self.bot.guilds):
            if not await config.enabled(guild.id, "streamer"):
                continue
            try:
                await self._lock_bets(guild)
                await self._activate_events(guild)
                await self.refresh_board(guild)
                await self._plan_jobs(guild)
            except Exception as exc:  # noqa: BLE001
                await capture_error(exc, guild_id=guild.id, command="streamhub.tick")

    @tick.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _plan(self, creds: dict, s: Streamer) -> list:
        hit = self._plans.get(s.id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            segs = await twitch.schedule(creds, s.platform_user_id)
        except PlatformError:
            segs = []
        self._plans[s.id] = (time.monotonic() + 1800, segs)
        return segs

    async def _plan_jobs(self, guild: discord.Guild) -> None:
        _, cfg = await self._ctx(guild.id)
        remind = int(cfg.get("remind_minutes") or 0)
        weekly_ch = self._text_channel(guild, cfg.id("weekly_plan_channel"))
        clip_week = cfg.get("clip_week", True) and self._text_channel(guild, cfg.id("clips_channel"))
        if not remind and not weekly_ch and not clip_week:
            return
        async with SessionLocal() as db:
            rows = (await db.execute(select(Streamer).where(Streamer.guild_id == guild.id, Streamer.platform == "twitch",
                                                            Streamer.enabled.is_(True), Streamer.platform_user_id.is_not(None)))).scalars().all()
        if not rows:
            return
        creds = await get_credentials(guild.id, "twitch")
        if not creds.get("client_id"):
            return
        gcfg = await config.get(guild.id, "general")
        now_local, week = local_now(gcfg.get("timezone")), week_key(gcfg.get("timezone"))
        if remind:
            await self._remind(guild, cfg, creds, rows, remind)
        if weekly_ch and now_local.weekday() == int(cfg.get("weekly_plan_day") or 0) and now_local.hour >= 10 \
                and await config.state(f"sh:plan:{guild.id}") != week:
            await config.set_state(f"sh:plan:{guild.id}", week)
            await self._post_week(guild, weekly_ch, creds, rows)
        if clip_week and now_local.weekday() == 6 and now_local.hour >= 18 and await config.state(f"sh:cotw:{guild.id}") != week:
            await config.set_state(f"sh:cotw:{guild.id}", week)
            await self._clip_of_week(guild, clip_week, creds, rows)

    async def _remind(self, guild: discord.Guild, cfg, creds: dict, rows: list[Streamer], minutes: int) -> None:
        _ = await i18n.for_guild(guild.id)
        now = utcnow()
        for s in rows:
            if s.is_live:
                continue
            for seg in await self._plan(creds, s):
                start = datetime.fromisoformat(seg["start_time"].replace("Z", "+00:00"))
                key = f"{s.id}:{seg['id']}:{seg['start_time']}"
                if key in self._reminded or not (now < start <= now + timedelta(minutes=minutes)):
                    continue
                self._reminded.add(key)
                ch = self._text_channel(guild, s.announce_channel_id or cfg.id("default_channel"))
                if ch is None:
                    continue
                th = await theme(guild)
                e = discord.Embed(title=_("sh.remind_title", streamer=s.display_name), url=stream_url(s),
                                  description=_("sh.remind_text", title=seg.get("title") or "—", game=(seg.get("category") or {}).get("name") or "—",
                                                when=ts(start, "R")), colour=th.color("primary"))
                if s.avatar_url:
                    e.set_thumbnail(url=s.avatar_url)
                role_id = s.ping_role_id or cfg.id("default_role")
                view = discord.ui.View()
                view.add_item(discord.ui.Button(label=_("stream.channel"), url=stream_url(s), emoji="📺"))
                try:
                    await ch.send(content=f"<@&{role_id}>" if role_id and cfg.get("remind_ping", False) else None, embed=e, view=view,
                                  allowed_mentions=discord.AllowedMentions(roles=True))
                except discord.HTTPException:
                    pass

    async def _post_week(self, guild: discord.Guild, ch: discord.TextChannel, creds: dict, rows: list[Streamer]) -> None:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        now, end = utcnow(), utcnow() + timedelta(days=7)
        items = []
        for s in rows:
            for seg in await self._plan(creds, s):
                start = datetime.fromisoformat(seg["start_time"].replace("Z", "+00:00"))
                if now <= start <= end:
                    items.append((start, s, seg))
        items.sort(key=lambda x: x[0])
        lines = [f"{ts(st, 'F')} · **[{s.display_name}]({stream_url(s)})**\n╰ {seg.get('title') or '—'}"
                 + (f" · {(seg.get('category') or {}).get('name')}" if (seg.get("category") or {}).get("name") else "")
                 for st, s, seg in items[:20]]
        e = discord.Embed(title=_("sh.week_title"), description="\n".join(lines) if lines else _("sh.week_empty"),
                          colour=th.color("primary"))
        try:
            await ch.send(embed=e)
        except discord.HTTPException:
            pass

    async def _clip_of_week(self, guild: discord.Guild, ch: discord.TextChannel, creds: dict, rows: list[Streamer]) -> None:
        _ = await i18n.for_guild(guild.id)
        best, owner = None, None
        for s in rows:
            try:
                clips = await twitch.clips(creds, s.platform_user_id, utcnow() - timedelta(days=7))
            except PlatformError:
                continue
            for c in clips:
                if best is None or c.get("view_count", 0) > best.get("view_count", 0):
                    best, owner = c, s
        if best is None:
            return
        th = await theme(guild)
        e = discord.Embed(title=_("sh.cotw_title"), url=best["url"], description=f"**{best['title']}**", colour=th.color("primary"))
        e.set_author(name=owner.display_name, icon_url=owner.avatar_url)
        e.set_image(url=best.get("thumbnail_url"))
        e.add_field(name=_("stream.clip_views"), value=fmt_num(best.get("view_count", 0)), inline=True)
        e.add_field(name=_("stream.clip_by"), value=best.get("creator_name", "—"), inline=True)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=_("stream.clip_watch"), url=best["url"], emoji="🎬"))
        try:
            await ch.send(embed=e, view=view)
        except discord.HTTPException:
            pass

    # ── Autocomplete ──
    async def _streamer_ac(self, interaction: discord.Interaction, current: str):
        async with SessionLocal() as db:
            rows = (await db.execute(select(Streamer).where(Streamer.guild_id == interaction.guild_id))).scalars().all()
        return [app_commands.Choice(name=(s.display_name or s.channel)[:100], value=s.id)
                for s in rows if current.lower() in (s.display_name or s.channel).lower()][:25]

    async def _get_streamer(self, guild_id: int, streamer_id: int) -> Streamer:
        async with SessionLocal() as db:
            s = await db.get(Streamer, streamer_id)
        if s is None or s.guild_id != guild_id:
            raise UserError("sh.no_streamer")
        return s

    # ── /stream ──
    @stream.command(name="info", description="Statistiken zu einem Streamer: Streams, Stunden, Zuschauer, Games, Streak")
    @app_commands.describe(streamer="Welcher Streamer?")
    @app_commands.autocomplete(streamer=_streamer_ac)
    async def stream_info(self, interaction: discord.Interaction, streamer: int):
        _ = await i18n.for_guild(interaction.guild_id)
        lang = await i18n.lang(interaction.guild_id)
        th = await theme(interaction.guild)
        s = await self._get_streamer(interaction.guild_id, streamer)
        async with SessionLocal() as db:
            sessions = (await db.execute(select(StreamSession).where(StreamSession.streamer_id == s.id)
                                         .order_by(StreamSession.started_at.desc()))).scalars().all()
            alarms = (await db.execute(select(func.count()).select_from(StreamAlert).where(StreamAlert.streamer_id == s.id))).scalar() or 0
        if not sessions:
            raise UserError("sh.no_sessions", streamer=s.display_name)
        secs = sum(((x.ended_at or utcnow()) - x.started_at).total_seconds() for x in sessions)
        avg = round(sum(x.avg_viewers for x in sessions) / len(sessions))
        games = Counter(x.game for x in sessions if x.game).most_common(3)
        e = discord.Embed(title=_("sh.info_title", streamer=s.display_name), url=stream_url(s), colour=th.color("primary"))
        if s.avatar_url:
            e.set_thumbnail(url=s.avatar_url)
        e.add_field(name=_("sh.f_streams"), value=fmt_num(len(sessions)), inline=True)
        e.add_field(name=_("sh.f_hours"), value=human_duration(secs, lang), inline=True)
        e.add_field(name=_("sh.f_streak"), value=_("sh.days", days=await self.streak(interaction.guild_id, s.id)), inline=True)
        e.add_field(name=_("stream.f_avg"), value=fmt_num(avg), inline=True)
        e.add_field(name=_("stream.f_peak"), value=fmt_num(max(x.peak_viewers for x in sessions)), inline=True)
        e.add_field(name=_("sh.f_checkins"), value=fmt_num(sum(x.checkins for x in sessions)), inline=True)
        if games:
            e.add_field(name=_("sh.f_games"), value="\n".join(f"• {g} ({n}×)" for g, n in games), inline=True)
        last = sessions[0]
        e.add_field(name=_("sh.f_last"), value=_("sh.live_now") if s.is_live else ts(last.started_at, "R"), inline=True)
        e.add_field(name=_("sh.f_alarms"), value=f"🔔 {alarms}", inline=True)
        await reply(interaction, e, ephemeral=False)

    @stream.command(name="uptime", description="Wer ist gerade live und seit wann?")
    async def stream_uptime(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        lang = await i18n.lang(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as db:
            live = (await db.execute(select(Streamer).where(Streamer.guild_id == interaction.guild_id, Streamer.is_live.is_(True)))).scalars().all()
            sessions = {x.id: x for x in (await db.execute(select(StreamSession).where(
                StreamSession.id.in_([s.current_session_id for s in live if s.current_session_id])))).scalars().all()}
        if not live:
            raise UserError("sh.nobody_live")
        lines = []
        for s in live:
            sess = sessions.get(s.current_session_id or 0)
            up = human_duration((utcnow() - sess.started_at).total_seconds(), lang) if sess else "—"
            lines.append(f"🔴 **[{s.display_name}]({stream_url(s)})** · ⏱️ {up} · 👀 {fmt_num(self._viewers.get(s.id, 0))}")
        await reply(interaction, th.embed(_("sh.uptime_title"), "\n".join(lines), icon=False), ephemeral=False)

    @stream.command(name="shoutout", description="Shoutout für einen Twitch-Streamer posten (z. B. nach einem Raid)")
    @app_commands.describe(twitch_name="Twitch-Name, z. B. corays_akh", nachricht="Eigener Text (optional)")
    async def stream_shoutout(self, interaction: discord.Interaction, twitch_name: str, nachricht: app_commands.Range[str, 0, 300] = ""):
        if not await can_host(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        login = re.sub(r"^(https?://)?(www\.)?twitch\.tv/", "", twitch_name.strip(), flags=re.I).strip("/@ ").lower()
        if not re.fullmatch(r"[a-z0-9_]{3,25}", login):
            raise UserError("stream.verify_bad_name")
        await interaction.response.defer()
        creds = await get_credentials(interaction.guild_id, "twitch")
        try:
            user = (await twitch.users(creds, [login])).get(login)
            if user is None:
                raise UserError("stream.verify_not_found", name=login)
            chan = await twitch.channel_info(creds, user.user_id) or {}
            live = (await twitch.streams(creds, [login])).get(login)
        except PlatformError:
            raise UserError("stream.verify_no_api") from None
        url = f"https://twitch.tv/{login}"
        text = nachricht or _("sh.shoutout_text", streamer=user.display_name)
        e = discord.Embed(title=_("sh.shoutout_title", streamer=user.display_name.upper()), url=url, description=text, colour=th.color("primary"))
        if user.avatar:
            e.set_thumbnail(url=user.avatar)
        e.add_field(name=_("sh.f_status"), value=_("sh.live_now") if live and live.live else _("sh.offline"), inline=True)
        if chan.get("game_name"):
            e.add_field(name=_("sh.f_last_game"), value=chan["game_name"], inline=True)
        if chan.get("title"):
            e.add_field(name=_("sh.f_title"), value=chan["title"][:1024], inline=False)
        if live and live.live and live.thumbnail:
            e.set_image(url=live.thumbnail)
        e.set_footer(text=_("sh.shoutout_by", user=interaction.user.display_name))
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=_("sh.follow"), url=url, emoji="💜"))
        await interaction.followup.send(embed=e, view=view)
        await log_event(interaction.guild_id, "stream", "shoutout", user=interaction.user, content=login)

    @stream.command(name="rang", description="Streamer-Rangliste: meiste Stunden in den letzten Tagen")
    @app_commands.describe(tage="Zeitraum in Tagen (Standard 30)")
    async def stream_rank(self, interaction: discord.Interaction, tage: app_commands.Range[int, 1, 365] = 30):
        _ = await i18n.for_guild(interaction.guild_id)
        lang = await i18n.lang(interaction.guild_id)
        th = await theme(interaction.guild)
        since = utcnow() - timedelta(days=tage)
        async with SessionLocal() as db:
            rows = (await db.execute(select(StreamSession, Streamer).join(Streamer, Streamer.id == StreamSession.streamer_id)
                                     .where(StreamSession.guild_id == interaction.guild_id, StreamSession.started_at >= since))).all()
        stats: dict[int, list] = {}
        for sess, s in rows:
            st = stats.setdefault(s.id, [s, 0.0, 0, 0])
            st[1] += ((sess.ended_at or utcnow()) - sess.started_at).total_seconds()
            st[2] += 1
            st[3] = max(st[3], sess.peak_viewers)
        if not stats:
            raise UserError("sh.rank_empty")
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`#{i + 1}`'} **{s.display_name}** · {human_duration(secs, lang)} · {n} Streams · 👀 max {fmt_num(peak)}"
                 for i, (s, secs, n, peak) in enumerate(sorted(stats.values(), key=lambda x: -x[1])[:15])]
        await reply(interaction, th.embed(_("sh.rank_title", days=tage), "\n".join(lines), icon=False), ephemeral=False)

    @stream.command(name="checkins", description="Deine Stream-Check-ins, Rang und nächste Stammzuschauer-Stufe")
    @app_commands.describe(user="Anderes Mitglied anzeigen")
    async def stream_checkins(self, interaction: discord.Interaction, user: discord.Member | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        cfg = await config.get(interaction.guild_id, "streamer")
        target = user or interaction.user
        async with SessionLocal() as db:
            row = await db.get(Member, (interaction.guild_id, target.id))
            count = row.stream_checkins if row else 0
            rank = (await db.execute(select(func.count()).select_from(Member).where(
                Member.guild_id == interaction.guild_id, Member.stream_checkins > count))).scalar() + 1
        tiers = sorted([t for t in cfg.get("loyalty_roles") or [] if t.get("role")], key=lambda t: t["checkins"])
        nxt = next((t for t in tiers if t["checkins"] > count), None)
        e = th.embed(_("sh.checkins_title", user=target.display_name), _("sh.checkins_text", count=count, rank=rank), icon=False)
        e.set_thumbnail(url=target.display_avatar.url)
        if nxt:
            e.add_field(name=_("sh.next_tier"), value=_("sh.next_tier_text", role=f"<@&{nxt['role']}>", left=nxt["checkins"] - count), inline=False)
        await reply(interaction, e, ephemeral=False)

    # ── /streamalarm ──
    @alarm.command(name="an", description="DM bekommen, sobald dieser Streamer live geht")
    @app_commands.autocomplete(streamer=_streamer_ac)
    async def alarm_on(self, interaction: discord.Interaction, streamer: int):
        _ = await i18n.for_guild(interaction.guild_id)
        s = await self._get_streamer(interaction.guild_id, streamer)
        async with session_scope() as db:
            if await db.get(StreamAlert, (interaction.user.id, s.id)) is None:
                db.add(StreamAlert(user_id=interaction.user.id, streamer_id=s.id, guild_id=interaction.guild_id))
        await reply(interaction, (await theme(interaction.guild)).success(_("sh.alarm_on_title"), _("sh.alarm_on", streamer=s.display_name)))

    @alarm.command(name="aus", description="Stream-Alarm für einen Streamer ausschalten")
    @app_commands.autocomplete(streamer=_streamer_ac)
    async def alarm_off(self, interaction: discord.Interaction, streamer: int):
        _ = await i18n.for_guild(interaction.guild_id)
        s = await self._get_streamer(interaction.guild_id, streamer)
        async with session_scope() as db:
            row = await db.get(StreamAlert, (interaction.user.id, s.id))
            if row is None:
                raise UserError("sh.alarm_not_set", streamer=s.display_name)
            await db.delete(row)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("sh.alarm_off", streamer=s.display_name)))

    @alarm.command(name="liste", description="Deine aktiven Stream-Alarme")
    async def alarm_list(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        async with SessionLocal() as db:
            rows = (await db.execute(select(Streamer).join(StreamAlert, StreamAlert.streamer_id == Streamer.id).where(
                StreamAlert.user_id == interaction.user.id, StreamAlert.guild_id == interaction.guild_id))).scalars().all()
        text = "\n".join(f"🔔 **{s.display_name}**" + (" · 🔴 live" if s.is_live else "") for s in rows) or _("sh.alarm_none")
        await reply(interaction, (await theme(interaction.guild)).embed(_("sh.alarm_list_title"), text, icon=False))

    # ── /wette ──
    def _bet_embed(self, _, th, p: Prediction, totals: dict[int, int], counts: dict[int, int]) -> discord.Embed:
        pot = sum(totals.values())
        lines = []
        for i, opt in enumerate(p.options):
            amount = totals.get(i, 0)
            pct = round(amount * 100 / pot) if pot else 0
            ratio = f" · 1:{pot / amount:.2f}" if amount else ""
            mark = "🏆 " if p.status == "done" and p.winner == i else ""
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            lines.append(f"{mark}**{opt}**\n`{bar}` {pct}% · 💰 {fmt_num(amount)} · 👥 {counts.get(i, 0)}{ratio}")
        status = {"open": _("sh.bet_open", until=ts(p.closes_at, "R")), "locked": _("sh.bet_locked"), "done": _("sh.bet_done"),
                  "cancelled": _("sh.bet_cancelled")}[p.status]
        e = discord.Embed(title=f"🎲 {p.question}", description="\n\n".join(lines) + f"\n\n{status}", colour=th.color("primary"))
        e.set_footer(text=_("sh.bet_footer", pot=fmt_num(pot), id=p.id))
        return e

    async def _bet_totals(self, pid: int) -> tuple[dict[int, int], dict[int, int]]:
        async with SessionLocal() as db:
            rows = (await db.execute(select(PredictionBet.option, func.sum(PredictionBet.amount), func.count())
                                     .where(PredictionBet.prediction_id == pid).group_by(PredictionBet.option))).all()
        return {o: int(a) for o, a, _n in rows}, {o: int(n) for o, _a, n in rows}

    async def _refresh_bet(self, guild: discord.Guild, pid: int) -> None:
        async with SessionLocal() as db:
            p = await db.get(Prediction, pid)
        if p is None or not p.message_id:
            return
        ch = guild.get_channel(p.channel_id)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        _ = await i18n.for_guild(guild.id)
        totals, counts = await self._bet_totals(pid)
        view = discord.ui.View(timeout=None)
        for i, opt in enumerate(p.options):
            view.add_item(BetButton(p.id, i, opt[:80], disabled=p.status != "open"))
        try:
            await ch.get_partial_message(p.message_id).edit(embed=self._bet_embed(_, await theme(guild), p, totals, counts), view=view)
        except discord.HTTPException:
            pass

    async def place_bet(self, interaction: discord.Interaction, pid: int, opt: int, raw: str) -> None:
        _, cfg = await self._ctx(interaction.guild_id)
        ecfg = await config.get(interaction.guild_id, "economy")
        digits = re.sub(r"[^\d]", "", raw or "")
        if not digits:
            raise UserError("sh.bet_bad_amount")
        amount = int(digits)
        lo, hi = int(cfg.get("bet_min") or 1), int(cfg.get("bet_max") or 10**9)
        if not lo <= amount <= hi:
            raise UserError("sh.bet_range", min=fmt_num(lo), max=fmt_num(hi))
        async with session_scope() as db:
            p = await db.get(Prediction, pid)
            if p is None or p.status != "open" or p.closes_at <= utcnow():
                raise UserError("sh.bet_closed")
            bet = await db.get(PredictionBet, (pid, interaction.user.id))
            if bet is not None and bet.option != opt:
                raise UserError("sh.bet_other_side", option=p.options[bet.option])
            member = await ensure_member(db, interaction.guild_id, interaction.user)
            if member.coins < amount:
                raise UserError("sh.bet_no_money", balance=fmt_num(member.coins), emoji=ecfg.get("currency_emoji") or "💰")
            member.coins -= amount
            if bet is None:
                db.add(PredictionBet(prediction_id=pid, user_id=interaction.user.id, option=opt, amount=amount))
                total = amount
            else:
                bet.amount += amount
                total = bet.amount
            option = p.options[opt]
        th = await theme(interaction.guild)
        await reply(interaction, th.success(_("sh.bet_placed_title"), _("sh.bet_placed", amount=fmt_num(amount), option=option, total=fmt_num(total),
                                                                         emoji=ecfg.get("currency_emoji") or "💰")))
        await self._refresh_bet(interaction.guild, pid)

    async def _lock_bets(self, guild: discord.Guild) -> None:
        async with session_scope() as db:
            due = (await db.execute(select(Prediction).where(Prediction.guild_id == guild.id, Prediction.status == "open",
                                                             Prediction.closes_at <= utcnow()))).scalars().all()
            for p in due:
                p.status = "locked"
            ids = [p.id for p in due]
        for pid in ids:
            await self._refresh_bet(guild, pid)

    async def _open_bet(self, interaction: discord.Interaction, include_locked: bool = True) -> Prediction:
        states = ["open", "locked"] if include_locked else ["open"]
        async with SessionLocal() as db:
            p = (await db.execute(select(Prediction).where(Prediction.guild_id == interaction.guild_id, Prediction.status.in_(states))
                                  .order_by(Prediction.id.desc()))).scalars().first()
        if p is None:
            raise UserError("sh.bet_none")
        return p

    async def _winner_ac(self, interaction: discord.Interaction, current: str):
        try:
            p = await self._open_bet(interaction)
        except UserError:
            return []
        return [app_commands.Choice(name=o[:100], value=i) for i, o in enumerate(p.options) if current.lower() in o.lower()][:25]

    @bet.command(name="start", description="Neue Stream-Wette starten – Zuschauer setzen Coins")
    @app_commands.describe(frage="Worauf wird gewettet?", optionen="Antworten, getrennt mit | (z. B. Win | Loss)", minuten="Wie lange darf gesetzt werden?")
    async def bet_start(self, interaction: discord.Interaction, frage: app_commands.Range[str, 3, 200], optionen: str,
                        minuten: app_commands.Range[int, 1, 1440] = 5):
        _, cfg = await self._ctx(interaction.guild_id)
        if not cfg.get("bets_enabled", True):
            raise UserError("sh.bets_disabled")
        if not await can_host(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        if not await config.enabled(interaction.guild_id, "economy"):
            raise UserError("sh.bets_need_economy")
        opts = [o.strip()[:80] for o in optionen.split("|") if o.strip()]
        if not 2 <= len(opts) <= 5 or len(set(o.lower() for o in opts)) != len(opts):
            raise UserError("sh.bet_bad_options")
        async with SessionLocal() as db:
            running = (await db.execute(select(func.count()).select_from(Prediction).where(
                Prediction.guild_id == interaction.guild_id, Prediction.status.in_(["open", "locked"])))).scalar()
        if running:
            raise UserError("sh.bet_running")
        async with session_scope() as db:
            p = Prediction(guild_id=interaction.guild_id, channel_id=interaction.channel_id, creator_id=interaction.user.id, question=frage,
                           options=opts, status="open", closes_at=utcnow() + timedelta(minutes=minuten))
            db.add(p)
            await db.flush()
            pid = p.id
        th = await theme(interaction.guild)
        view = discord.ui.View(timeout=None)
        for i, opt in enumerate(opts):
            view.add_item(BetButton(pid, i, opt))
        await interaction.response.send_message(embed=self._bet_embed(_, th, p, {}, {}), view=view)
        msg = await interaction.original_response()
        async with session_scope() as db:
            (await db.get(Prediction, pid)).message_id = msg.id
        await log_event(interaction.guild_id, "stream", "bet_start", user=interaction.user, content=frage)

    @bet.command(name="ende", description="Wette auflösen und Gewinner auszahlen")
    @app_commands.describe(gewinner="Welche Antwort hat gewonnen?")
    @app_commands.autocomplete(gewinner=_winner_ac)
    async def bet_end(self, interaction: discord.Interaction, gewinner: int):
        _ = await i18n.for_guild(interaction.guild_id)
        if not await can_host(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        p = await self._open_bet(interaction)
        if not 0 <= gewinner < len(p.options):
            raise UserError("sh.bet_bad_winner")
        await interaction.response.defer()
        async with session_scope() as db:
            row = await db.get(Prediction, p.id)
            if row.status not in ("open", "locked"):
                raise UserError("sh.bet_none")
            row.status, row.winner = "done", gewinner
            bets = [(b.user_id, b.option, b.amount) for b in (await db.execute(select(PredictionBet).where(PredictionBet.prediction_id == p.id))).scalars()]
        payouts = split_pot(bets, gewinner)
        for uid, amount in payouts.items():
            await add_coins(interaction.guild_id, uid, amount, reason="bet")
        await self._refresh_bet(interaction.guild, p.id)
        th = await theme(interaction.guild)
        ecfg = await config.get(interaction.guild_id, "economy")
        winners = [(u, a) for u, o, a in bets if o == gewinner]
        top = sorted(((u, payouts[u]) for u, _a in winners), key=lambda x: -x[1])[:5]
        text = _("sh.bet_result", option=p.options[gewinner], winners=len(winners), pot=fmt_num(sum(a for *_x, a in bets)), emoji=ecfg.get("currency_emoji") or "💰")
        if not winners and bets:
            text += "\n" + _("sh.bet_refunded")
        if top:
            text += "\n\n" + "\n".join(f"• <@{u}> +{fmt_num(a)}" for u, a in top)
        await interaction.followup.send(embed=th.success(_("sh.bet_result_title"), text), allowed_mentions=discord.AllowedMentions.none())

    @bet.command(name="abbrechen", description="Laufende Wette abbrechen – alle bekommen ihren Einsatz zurück")
    async def bet_cancel(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        if not await can_host(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        p = await self._open_bet(interaction)
        async with session_scope() as db:
            row = await db.get(Prediction, p.id)
            row.status = "cancelled"
            bets = (await db.execute(select(PredictionBet).where(PredictionBet.prediction_id == p.id))).scalars().all()
            refunds = [(b.user_id, b.amount) for b in bets]
        for uid, amount in refunds:
            await add_coins(interaction.guild_id, uid, amount, reason="bet_refund")
        await self._refresh_bet(interaction.guild, p.id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("sh.bet_cancel_done", count=len(refunds))))


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(BetButton)
    await bot.add_cog(StreamHub(bot))
