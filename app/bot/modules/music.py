"""Musik: Queue, Loop, Shuffle, Lautstärke, Skip-Vote, Now-Playing-Panel mit Buttons, Spotify-Links, Dashboard-Steuerung.
Benötigt FFmpeg (im Docker-Image enthalten) und yt-dlp."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from app.bot.ui import BaseView, Paginator, chunk, handle_exception, reply
from app.core.embeds import progress_bar, theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import i18n
from app.services.integrations import get_credentials
from app.services.notify import is_admin

log = logging.getLogger("nova.music")

YTDL_OPTS = {
    "format": "bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True, "default_search": "ytsearch",
    "source_address": "0.0.0.0", "extract_flat": False, "skip_download": True,
}
FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
FFMPEG_OPTS = "-vn -loglevel error"
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?(track|playlist|album)/([A-Za-z0-9]+)")


@dataclass
class Track:
    title: str
    url: str
    duration: int = 0
    thumbnail: str | None = None
    requester_id: int = 0
    search: str | None = None  # für Spotify: erst beim Abspielen auflösen
    stream_url: str | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("stream_url", None)
        d["requester_id"] = str(self.requester_id)
        return d


def _fmt(seconds: int) -> str:
    if not seconds:
        return "🔴 Live"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _ytdl(query: str, playlist: bool = False) -> dict[str, Any]:
    import yt_dlp

    opts = dict(YTDL_OPTS)
    if playlist:
        opts.update(noplaylist=False, extract_flat="in_playlist", playlistend=100)

    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)

    return await asyncio.get_running_loop().run_in_executor(None, run)


class SpotifyClient:
    def __init__(self) -> None:
        self._token: dict[int, tuple[str, float]] = {}

    async def _auth(self, guild_id: int) -> str:
        cached = self._token.get(guild_id)
        if cached and cached[1] > time.time():
            return cached[0]
        creds = await get_credentials(guild_id, "spotify")
        if not creds.get("client_id") or not creds.get("client_secret"):
            raise UserError("music.spotify_missing")
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                             auth=(creds["client_id"], creds["client_secret"]))
        if r.status_code != 200:
            raise UserError("music.spotify_failed")
        data = r.json()
        self._token[guild_id] = (data["access_token"], time.time() + data.get("expires_in", 3600) - 60)
        return data["access_token"]

    async def resolve(self, guild_id: int, kind: str, sid: str, limit: int) -> list[tuple[str, int]]:
        token = await self._auth(guild_id)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=15, headers=headers) as c:
            if kind == "track":
                t = (await c.get(f"https://api.spotify.com/v1/tracks/{sid}")).json()
                return [(f"{t['artists'][0]['name']} - {t['name']}", t.get("duration_ms", 0) // 1000)]
            url = f"https://api.spotify.com/v1/{kind}s/{sid}/tracks?limit=100"
            r = await c.get(url)
            if r.status_code != 200:
                raise UserError("music.spotify_failed")
            out = []
            for it in r.json().get("items", []):
                t = it.get("track") or it
                if t and t.get("name"):
                    out.append((f"{t['artists'][0]['name']} - {t['name']}", t.get("duration_ms", 0) // 1000))
                if len(out) >= limit:
                    break
            return out


class Player:
    def __init__(self, cog: "Music", guild: discord.Guild):
        self.cog, self.guild = cog, guild
        self.queue: list[Track] = []
        self.current: Track | None = None
        self.loop_mode = "off"  # off | track | queue
        self.volume = 0.6
        self.skip_votes: set[int] = set()
        self.text_channel: discord.abc.Messageable | None = None
        self.np_message: discord.Message | None = None
        self.started_at = 0.0
        self.paused_at = 0.0
        self.idle_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client  # type: ignore[return-value]

    def position(self) -> int:
        if not self.current or not self.started_at:
            return 0
        end = self.paused_at or time.monotonic()
        return int(end - self.started_at)

    def state(self) -> dict[str, Any]:
        vc = self.vc
        return {
            "connected": bool(vc and vc.is_connected()), "channel": vc.channel.name if vc and vc.channel else None,
            "channel_id": str(vc.channel.id) if vc and vc.channel else None,
            "paused": bool(vc and vc.is_paused()), "playing": bool(vc and vc.is_playing()),
            "current": self.current.public() if self.current else None, "position": self.position(),
            "queue": [t.public() for t in self.queue[:100]], "queue_length": len(self.queue),
            "loop": self.loop_mode, "volume": int(self.volume * 100),
            "listeners": len([m for m in vc.channel.members if not m.bot]) if vc and vc.channel else 0,
        }

    def publish(self) -> None:
        bus.publish(self.guild.id, "music", self.state())

    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        if self.vc and self.vc.is_connected():
            if self.vc.channel.id != channel.id:
                if any(not m.bot for m in self.vc.channel.members) and self.current:
                    raise UserError("music.busy_elsewhere")
                await self.vc.move_to(channel)
            return
        await channel.connect(self_deaf=True, timeout=20)

    async def play_next(self) -> None:
        async with self.lock:
            vc = self.vc
            if vc is None or not vc.is_connected():
                return
            if self.current and self.loop_mode == "track":
                nxt = self.current
            else:
                if self.current and self.loop_mode == "queue":
                    self.queue.append(self.current)
                nxt = self.queue.pop(0) if self.queue else None
            self.skip_votes.clear()
            self.current = nxt
            if nxt is None:
                self.started_at = 0
                self.publish()
                await self._update_np(ended=True)
                self._schedule_idle()
                return
            if self.idle_task:
                self.idle_task.cancel()
            try:
                await self._resolve(nxt)
            except Exception as exc:  # noqa: BLE001
                log.warning("Track konnte nicht geladen werden (%s): %s", nxt.title, exc)
                self.current = None
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.play_next()))
                return
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(nxt.stream_url, before_options=FFMPEG_BEFORE, options=FFMPEG_OPTS), volume=self.volume)
            loop = asyncio.get_running_loop()

            def after(err: Exception | None):
                if err:
                    log.warning("Player-Fehler: %s", err)
                asyncio.run_coroutine_threadsafe(self.play_next(), loop)

            vc.play(source, after=after)
            self.started_at, self.paused_at = time.monotonic(), 0
            self.publish()
            await self._update_np()

    async def _resolve(self, t: Track) -> None:
        if t.search and not t.stream_url:
            info = await _ytdl(f"ytsearch1:{t.search}")
            info = info["entries"][0] if info.get("entries") else info
            t.url, t.thumbnail = info.get("webpage_url") or t.url, info.get("thumbnail")
            t.duration = int(info.get("duration") or t.duration or 0)
            t.stream_url = info.get("url")
            return
        info = await _ytdl(t.url)
        info = info["entries"][0] if info.get("entries") else info
        t.stream_url = info.get("url")
        t.thumbnail = t.thumbnail or info.get("thumbnail")
        t.duration = t.duration or int(info.get("duration") or 0)

    def _schedule_idle(self) -> None:
        if self.idle_task:
            self.idle_task.cancel()

        async def idle():
            cfg = await config.get(self.guild.id, "music")
            await asyncio.sleep(cfg.get("idle_minutes", 3) * 60)
            if self.vc and not self.vc.is_playing():
                await self.cog.destroy(self.guild)

        self.idle_task = asyncio.create_task(idle())

    async def np_embed(self) -> discord.Embed:
        _ = await i18n.for_guild(self.guild.id)
        th = await theme(self.guild)
        t = self.current
        if t is None:
            return th.info(_("music.np_title"), _("music.nothing"))
        pos = self.position()
        bar = progress_bar(pos, t.duration or 1, 16) if t.duration else "🔴 LIVE"
        e = th.embed(_("music.np_title"), f"**[{t.title}]({t.url})**\n\n{bar}\n`{_fmt(pos)} / {_fmt(t.duration)}`", icon=False)
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        e.add_field(name=_("music.requested_by"), value=f"<@{t.requester_id}>", inline=True)
        e.add_field(name=_("music.volume"), value=f"🔊 {int(self.volume * 100)}%", inline=True)
        e.add_field(name=_("music.loop"), value={"off": "➡️ Aus", "track": "🔂 Track", "queue": "🔁 Queue"}[self.loop_mode], inline=True)
        if self.queue:
            e.add_field(name=_("music.up_next"), value=f"1. {self.queue[0].title[:80]}" + (f"\n+{len(self.queue) - 1}" if len(self.queue) > 1 else ""), inline=False)
        return e

    async def _update_np(self, ended: bool = False) -> None:
        if self.text_channel is None:
            return
        try:
            if self.np_message:
                try:
                    await self.np_message.delete()
                except discord.HTTPException:
                    pass
                self.np_message = None
            if not ended:
                self.np_message = await self.text_channel.send(embed=await self.np_embed(), view=MusicControls(self.cog, self.guild.id))
        except discord.HTTPException:
            pass


class MusicControls(discord.ui.View):
    def __init__(self, cog: "Music", guild_id: int):
        super().__init__(timeout=None)
        self.cog, self.guild_id = cog, guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            await self.cog.require_listener(interaction)
            return True
        except UserError as exc:
            await handle_exception(interaction, exc)
            return False

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await handle_exception(interaction, error, "music_controls")

    async def _refresh(self, interaction: discord.Interaction):
        p = self.cog.players.get(self.guild_id)
        if p:
            await interaction.response.edit_message(embed=await p.np_embed(), view=self)
            p.publish()

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
    async def pause(self, interaction: discord.Interaction, _b):
        await self.cog.control(interaction.guild, "toggle")
        await self._refresh(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, _b):
        msg = await self.cog.request_skip(interaction)
        await reply(interaction, msg)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, _b):
        await self.cog.require_dj(interaction)
        await interaction.response.defer()
        await self.cog.destroy(interaction.guild)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, _b):
        await self.cog.control(interaction.guild, "loop")
        await self._refresh(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction: discord.Interaction, _b):
        await self.cog.control(interaction.guild, "shuffle")
        await self._refresh(interaction)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, _b):
        p = self.cog.players.get(self.guild_id)
        await self.cog.control(interaction.guild, "volume", int((p.volume if p else 0.6) * 100) - 10)
        await self._refresh(interaction)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, _b):
        p = self.cog.players.get(self.guild_id)
        await self.cog.control(interaction.guild, "volume", int((p.volume if p else 0.6) * 100) + 10)
        await self._refresh(interaction)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue(self, interaction: discord.Interaction, _b):
        await self.cog.send_queue(interaction)


@app_commands.guild_only()
class Music(commands.Cog):
    module = "music"
    help_category = "music"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, Player] = {}
        self.spotify = SpotifyClient()

    def player(self, guild: discord.Guild) -> Player:
        if guild.id not in self.players:
            self.players[guild.id] = Player(self, guild)
        return self.players[guild.id]

    async def destroy(self, guild: discord.Guild) -> None:
        p = self.players.pop(guild.id, None)
        if p:
            p.queue.clear()
            p.current = None
            if p.idle_task:
                p.idle_task.cancel()
            await p._update_np(ended=True)
        if guild.voice_client:
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
        bus.publish(guild.id, "music", {"connected": False, "queue": [], "current": None})

    # ── Rechte ──
    async def _is_dj(self, member: discord.Member) -> bool:
        cfg = await config.get(member.guild.id, "music")
        return await is_admin(member) or bool(cfg.id("dj_role") and member.get_role(cfg.id("dj_role")))

    async def require_listener(self, interaction: discord.Interaction) -> discord.VoiceChannel:
        member: discord.Member = interaction.user  # type: ignore[assignment]
        cfg = await config.get(interaction.guild_id, "music")
        allowed = cfg.ids("allowed_channels")
        if allowed and interaction.channel_id not in allowed:
            raise UserError("music.wrong_channel")
        if not member.voice or not member.voice.channel:
            raise UserError("music.join_voice")
        vc = interaction.guild.voice_client
        if vc and vc.channel and vc.channel.id != member.voice.channel.id and not await self._is_dj(member):
            raise UserError("music.same_channel")
        return member.voice.channel  # type: ignore[return-value]

    async def require_dj(self, interaction: discord.Interaction) -> None:
        member: discord.Member = interaction.user  # type: ignore[assignment]
        p = self.players.get(interaction.guild_id)
        if await self._is_dj(member):
            return
        listeners = [m for m in (member.voice.channel.members if member.voice and member.voice.channel else []) if not m.bot]
        if p and p.current and p.current.requester_id == member.id:
            return
        if len(listeners) <= 1:
            return
        raise UserError("music.dj_only")

    # ── Laden ──
    async def load_tracks(self, guild: discord.Guild, query: str, requester_id: int) -> list[Track]:
        cfg = await config.get(guild.id, "music")
        limit = cfg.get("max_queue", 200)
        sp = SPOTIFY_RE.search(query)
        if sp:
            kind, sid = sp.groups()
            if kind != "track" and not cfg.get("allow_playlists", True):
                raise UserError("music.no_playlists")
            items = await self.spotify.resolve(guild.id, kind, sid, limit)
            return [Track(title=name, url=f"https://open.spotify.com/{kind}/{sid}", duration=d, requester_id=requester_id, search=name) for name, d in items]
        is_url = query.startswith(("http://", "https://"))
        is_playlist = is_url and ("list=" in query or "/playlist" in query or "/sets/" in query)
        if is_playlist and not cfg.get("allow_playlists", True):
            raise UserError("music.no_playlists")
        try:
            info = await _ytdl(query if is_url else f"ytsearch1:{query}", playlist=is_playlist)
        except Exception as exc:  # noqa: BLE001
            log.info("yt-dlp: %s", exc)
            raise UserError("music.not_found")
        entries = info.get("entries") if info.get("entries") is not None else [info]
        tracks = []
        max_secs = int(cfg.get("max_minutes") or 0) * 60
        for e in entries:
            if not e:
                continue
            dur = int(e.get("duration") or 0)
            if max_secs and dur > max_secs:
                continue
            url = e.get("webpage_url") or e.get("url") or ""
            if not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={e.get('id')}"
            tracks.append(Track(title=e.get("title") or "Unbekannt", url=url, duration=dur,
                                thumbnail=e.get("thumbnail") or (e.get("thumbnails") or [{}])[-1].get("url"), requester_id=requester_id))
            if len(tracks) >= limit:
                break
        if not tracks:
            raise UserError("music.not_found")
        return tracks

    async def enqueue(self, guild: discord.Guild, channel: discord.VoiceChannel, text_channel, query: str, requester_id: int) -> list[Track]:
        if not shutil.which("ffmpeg"):
            raise UserError("music.no_ffmpeg")
        cfg = await config.get(guild.id, "music")
        p = self.player(guild)
        if len(p.queue) >= cfg.get("max_queue", 200):
            raise UserError("music.queue_full")
        tracks = await self.load_tracks(guild, query, requester_id)
        tracks = tracks[: max(0, cfg.get("max_queue", 200) - len(p.queue))]
        await p.connect(channel)
        if p.volume == 0.6 and not p.current:
            p.volume = cfg.get("default_volume", 60) / 100
        p.text_channel = text_channel or p.text_channel
        p.queue.extend(tracks)
        if not p.vc.is_playing() and not p.vc.is_paused() and p.current is None:
            await p.play_next()
        else:
            p.publish()
        return tracks

    async def control(self, guild: discord.Guild, action: str, value: Any = None) -> dict[str, Any]:
        """Zentrale Steuerung – auch vom Dashboard genutzt."""
        p = self.players.get(guild.id)
        if p is None or p.vc is None:
            raise UserError("music.not_playing")
        vc = p.vc
        if action == "toggle":
            action = "resume" if vc.is_paused() else "pause"
        if action == "pause" and vc.is_playing():
            vc.pause()
            p.paused_at = time.monotonic()
        elif action == "resume" and vc.is_paused():
            vc.resume()
            if p.paused_at:
                p.started_at += time.monotonic() - p.paused_at
            p.paused_at = 0
        elif action == "skip":
            if p.loop_mode == "track":
                p.current = None
            vc.stop()
        elif action == "stop":
            await self.destroy(guild)
            return {"connected": False}
        elif action == "volume":
            p.volume = max(0.01, min(1.5, int(value) / 100))
            if isinstance(vc.source, discord.PCMVolumeTransformer):
                vc.source.volume = p.volume
        elif action == "loop":
            p.loop_mode = value if value in ("off", "track", "queue") else {"off": "track", "track": "queue", "queue": "off"}[p.loop_mode]
        elif action == "shuffle":
            random.shuffle(p.queue)
        elif action == "remove":
            idx = int(value) - 1
            if not 0 <= idx < len(p.queue):
                raise UserError("music.bad_index")
            p.queue.pop(idx)
        elif action == "clear":
            p.queue.clear()
        elif action == "move":
            a, b = (int(x) - 1 for x in value)
            if not (0 <= a < len(p.queue) and 0 <= b < len(p.queue)):
                raise UserError("music.bad_index")
            p.queue.insert(b, p.queue.pop(a))
        p.publish()
        return p.state()

    async def request_skip(self, interaction: discord.Interaction) -> discord.Embed:
        await self.require_listener(interaction)
        p = self.players.get(interaction.guild_id)
        if not p or not p.current:
            raise UserError("music.not_playing")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        member: discord.Member = interaction.user  # type: ignore[assignment]
        if await self._is_dj(member) or p.current.requester_id == member.id:
            await self.control(interaction.guild, "skip")
            return th.success(_("music.skipped_title"), _("music.skipped"))
        cfg = await config.get(interaction.guild_id, "music")
        listeners = [m for m in p.vc.channel.members if not m.bot]
        needed = max(1, -(-len(listeners) * cfg.get("skip_vote_percent", 50) // 100))
        p.skip_votes.add(member.id)
        if len(p.skip_votes) >= needed:
            await self.control(interaction.guild, "skip")
            return th.success(_("music.skipped_title"), _("music.vote_passed"))
        return th.info(_("music.vote_title"), _("music.vote", votes=len(p.skip_votes), needed=needed))

    async def send_queue(self, interaction: discord.Interaction) -> None:
        p = self.players.get(interaction.guild_id)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        if not p or (not p.current and not p.queue):
            await reply(interaction, th.info(_("music.queue_title"), _("music.queue_empty")))
            return
        pages = []
        total = sum(t.duration for t in p.queue)
        for pi, group in enumerate(chunk(p.queue, 10) if p.queue else [[]]):
            lines = [f"`{i}.` [{t.title[:70]}]({t.url}) · `{_fmt(t.duration)}` · <@{t.requester_id}>" for i, t in enumerate(group, pi * 10 + 1)]
            e = th.embed(_("music.queue_title"), "\n".join(lines) or _("music.queue_empty"), icon=False)
            if p.current:
                e.add_field(name=_("music.np_title"), value=f"[{p.current.title[:100]}]({p.current.url})", inline=False)
            e.set_footer(text=_("music.queue_footer", count=len(p.queue), duration=_fmt(total)))
            pages.append(e)
        await Paginator(interaction.user.id, pages).send(interaction, ephemeral=True)

    # ── Commands ──
    @app_commands.command(name="play", description="Spielt einen Song oder eine Playlist (YouTube, SoundCloud, Spotify-Links …)")
    @app_commands.describe(query="Suchbegriff oder Link")
    async def play(self, interaction: discord.Interaction, query: app_commands.Range[str, 1, 400]):
        channel = await self.require_listener(interaction)
        await interaction.response.defer(thinking=True)
        tracks = await self.enqueue(interaction.guild, channel, interaction.channel, query, interaction.user.id)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        if len(tracks) == 1:
            t = tracks[0]
            e = th.success(_("music.added_title"), f"[{t.title}]({t.url}) · `{_fmt(t.duration)}`")
            if t.thumbnail:
                e.set_thumbnail(url=t.thumbnail)
        else:
            e = th.success(_("music.added_title"), _("music.added_many", count=len(tracks)))
        await interaction.followup.send(embed=e)

    @app_commands.command(name="pause", description="Pausiert die Wiedergabe")
    async def pause(self, interaction: discord.Interaction):
        await self.require_listener(interaction)
        await self.control(interaction.guild, "pause")
        await self._ok(interaction, "music.paused")

    @app_commands.command(name="resume", description="Setzt die Wiedergabe fort")
    async def resume(self, interaction: discord.Interaction):
        await self.require_listener(interaction)
        await self.control(interaction.guild, "resume")
        await self._ok(interaction, "music.resumed")

    @app_commands.command(name="skip", description="Überspringt den aktuellen Song (ggf. per Vote)")
    async def skip(self, interaction: discord.Interaction):
        await reply(interaction, await self.request_skip(interaction), ephemeral=False)

    @app_commands.command(name="stop", description="Stoppt die Musik und leert die Queue")
    async def stop(self, interaction: discord.Interaction):
        await self.require_listener(interaction)
        await self.require_dj(interaction)
        await self.destroy(interaction.guild)
        await self._ok(interaction, "music.stopped")

    @app_commands.command(name="queue", description="Zeigt die Warteschlange")
    async def queue(self, interaction: discord.Interaction):
        await self.send_queue(interaction)

    @app_commands.command(name="volume", description="Setzt die Lautstärke (1–150)")
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 1, 150]):
        await self.require_listener(interaction)
        await self.require_dj(interaction)
        await self.control(interaction.guild, "volume", percent)
        await self._ok(interaction, "music.volume_set", volume=percent)

    @app_commands.command(name="loop", description="Loop-Modus: aus, Track oder Queue")
    @app_commands.choices(mode=[app_commands.Choice(name="➡️ Aus", value="off"), app_commands.Choice(name="🔂 Track", value="track"),
                                app_commands.Choice(name="🔁 Queue", value="queue")])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await self.require_listener(interaction)
        await self.control(interaction.guild, "loop", mode.value)
        await self._ok(interaction, "music.loop_set", mode=mode.name)

    @app_commands.command(name="shuffle", description="Mischt die Warteschlange")
    async def shuffle(self, interaction: discord.Interaction):
        await self.require_listener(interaction)
        await self.control(interaction.guild, "shuffle")
        await self._ok(interaction, "music.shuffled")

    @app_commands.command(name="nowplaying", description="Zeigt den aktuellen Song")
    async def nowplaying(self, interaction: discord.Interaction):
        p = self.players.get(interaction.guild_id)
        if not p or not p.current:
            raise UserError("music.not_playing")
        await reply(interaction, await p.np_embed(), view=MusicControls(self, interaction.guild_id), ephemeral=False)

    @app_commands.command(name="remove", description="Entfernt einen Song aus der Queue")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 1000]):
        await self.require_listener(interaction)
        await self.require_dj(interaction)
        await self.control(interaction.guild, "remove", position)
        await self._ok(interaction, "music.removed", position=position)

    async def _ok(self, interaction: discord.Interaction, key: str, **kw):
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _(key, **kw)), ephemeral=False)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if self.bot.user and member.id == self.bot.user.id and before.channel and not after.channel:
            p = self.players.pop(member.guild.id, None)
            if p:
                p.queue.clear()
                p.current = None
                bus.publish(member.guild.id, "music", {"connected": False, "queue": [], "current": None})
            return
        vc = member.guild.voice_client
        if vc and vc.channel and before.channel and before.channel.id == vc.channel.id:
            if not [m for m in vc.channel.members if not m.bot]:
                p = self.players.get(member.guild.id)
                if p:
                    p._schedule_idle()


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
