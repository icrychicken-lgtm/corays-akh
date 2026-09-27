"""Join-to-Create (temporäre Voice-Channels mit Steuer-Panel) und automatische Stats-Channels."""
from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord.ext import commands, tasks
from sqlalchemy import select

from app.bot.ui import handle_exception, reply
from app.config import settings
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import fill, i18n
from app.db.base import SessionLocal, session_scope
from app.db.models import Streamer, TempChannel

log = logging.getLogger("nova.voice")


async def _temp(channel_id: int) -> TempChannel:
    async with SessionLocal() as db:
        t = await db.get(TempChannel, channel_id)
    if t is None:
        raise UserError("tv.not_temp")
    return t


class TempAction(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:tv:(?P<action>rename|limit|lock|kick|block|delete)"):
    EMOJI = {"rename": "✏️", "limit": "👥", "lock": "🔒", "kick": "👢", "block": "🚫", "delete": "🗑️"}

    def __init__(self, action: str, label: str | None = None):
        style = discord.ButtonStyle.danger if action == "delete" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=label, emoji=self.EMOJI[action], style=style, custom_id=f"nova:tv:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction):
        try:
            t = await _temp(interaction.channel_id)
            member: discord.Member = interaction.user  # type: ignore[assignment]
            if t.owner_id != member.id and not member.guild_permissions.manage_channels:
                raise UserError("tv.owner_only")
            channel: discord.VoiceChannel = interaction.channel  # type: ignore[assignment]
            _ = await i18n.for_guild(interaction.guild_id)
            if self.action == "rename":
                await interaction.response.send_modal(TempModal("rename", _("tv.rename"), _("tv.name"), channel.name))
            elif self.action == "limit":
                await interaction.response.send_modal(TempModal("limit", _("tv.limit"), _("tv.limit_label"), str(channel.user_limit)))
            elif self.action == "lock":
                ow = channel.overwrites_for(interaction.guild.default_role)
                locked = ow.connect is False
                ow.connect = None if locked else False
                await channel.set_permissions(interaction.guild.default_role, overwrite=ow)
                await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("tv.unlocked" if locked else "tv.locked")))
            elif self.action in ("kick", "block"):
                view = discord.ui.View(timeout=60)
                view.add_item(TempUserSelect(self.action, _("tv.pick_user")))
                await reply(interaction, None, content=_("tv.pick_user"), view=view)
            elif self.action == "delete":
                await interaction.response.defer(ephemeral=True)
                await channel.delete(reason="Temp-Channel gelöscht")
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "tempvoice")


class TempModal(discord.ui.Modal):
    def __init__(self, action: str, title: str, label: str, current: str):
        super().__init__(title=title[:45])
        self.action = action
        self.value = discord.ui.TextInput(label=label[:45], default=current[:90], max_length=90 if action == "rename" else 2)
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction):
        channel: discord.VoiceChannel = interaction.channel  # type: ignore[assignment]
        _ = await i18n.for_guild(interaction.guild_id)
        if self.action == "rename":
            await channel.edit(name=self.value.value.strip()[:90] or channel.name)
        else:
            if not self.value.value.strip().isdigit() or int(self.value.value) > 99:
                raise UserError("tv.bad_limit")
            await channel.edit(user_limit=int(self.value.value))
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("common.done")))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "tempvoice_modal")


class TempUserSelect(discord.ui.UserSelect):
    def __init__(self, action: str, placeholder: str):
        super().__init__(placeholder=placeholder, min_values=1, max_values=1)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        try:
            t = await _temp(interaction.channel_id)
            if t.owner_id != interaction.user.id and not interaction.user.guild_permissions.manage_channels:  # type: ignore[union-attr]
                raise UserError("tv.owner_only")
            target = self.values[0]
            channel: discord.VoiceChannel = interaction.channel  # type: ignore[assignment]
            if target.id == t.owner_id:
                raise UserError("tv.not_self")
            if isinstance(target, discord.Member) and target.voice and target.voice.channel and target.voice.channel.id == channel.id:
                await target.move_to(None, reason="Temp-Channel Kick")
            if self.action == "block":
                await channel.set_permissions(target, connect=False, view_channel=True)
                async with session_scope() as db:
                    row = await db.get(TempChannel, channel.id)
                    row.blocked_ids = list(dict.fromkeys([*(row.blocked_ids or []), str(target.id)]))
            _ = await i18n.for_guild(interaction.guild_id)
            await interaction.response.edit_message(content=_("tv.done_" + self.action, user=target.mention), view=None)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "tempvoice_user")


class TempVoice(commands.Cog):
    module = "tempvoice"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._creating: set[int] = set()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if before.channel and before.channel != after.channel:
            await self._cleanup(before.channel)
        if member.bot or not after.channel or after.channel == before.channel:
            return
        if not await config.enabled(guild.id, "tempvoice"):
            return
        cfg = await config.get(guild.id, "tempvoice")
        if after.channel.id != cfg.id("hub_channel") or member.id in self._creating:
            return
        self._creating.add(member.id)
        try:
            category = guild.get_channel(cfg.id("category") or 0) or after.channel.category
            name = fill(cfg.get("name_template") or "🔊 {username}", username=member.display_name, user=member.name)[:95]
            overwrites = dict(category.overwrites) if isinstance(category, discord.CategoryChannel) else {}
            overwrites[member] = discord.PermissionOverwrite(connect=True, manage_channels=True, move_members=True, view_channel=True)
            overwrites[guild.me] = discord.PermissionOverwrite(connect=True, manage_channels=True, move_members=True, view_channel=True, send_messages=True)
            channel = await guild.create_voice_channel(name, category=category if isinstance(category, discord.CategoryChannel) else None,
                                                       overwrites=overwrites, user_limit=cfg.get("default_limit", 0), reason="Join-to-Create")
            async with session_scope() as db:
                db.add(TempChannel(channel_id=channel.id, guild_id=guild.id, owner_id=member.id, blocked_ids=[]))
            await member.move_to(channel)
            await self._panel(channel, member)
        except discord.HTTPException as exc:
            log.warning("Temp-Channel konnte nicht erstellt werden: %s", exc)
        finally:
            self._creating.discard(member.id)

    async def _panel(self, channel: discord.VoiceChannel, owner: discord.Member) -> None:
        _ = await i18n.for_guild(channel.guild.id)
        th = await theme(channel.guild)
        e = th.embed(_("tv.panel_title"), _("tv.panel_desc", user=owner.mention), icon=False)
        v = discord.ui.View(timeout=None)
        for a in ("rename", "limit", "lock", "kick", "block", "delete"):
            v.add_item(TempAction(a, _("tv." + a)))
        try:
            await channel.send(embed=e, view=v)
        except discord.HTTPException:
            pass

    async def _cleanup(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.VoiceChannel) or any(not m.bot for m in channel.members):
            return
        async with SessionLocal() as db:
            t = await db.get(TempChannel, channel.id)
        if t is None:
            return
        try:
            await channel.delete(reason="Temp-Channel leer")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        async with session_scope() as db:
            t = await db.get(TempChannel, channel.id)
            if t:
                await db.delete(t)

    @commands.Cog.listener()
    async def on_ready(self):
        async with SessionLocal() as db:
            rows = (await db.execute(select(TempChannel))).scalars().all()
        for t in rows:
            ch = self.bot.get_channel(t.channel_id)
            if ch is None:
                async with session_scope() as db:
                    row = await db.get(TempChannel, t.channel_id)
                    if row:
                        await db.delete(row)
            else:
                await self._cleanup(ch)


class StatsChannels(commands.Cog):
    module = "stats_channels"
    help_category = "admin"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.loop.start()

    def cog_unload(self):
        self.loop.cancel()

    async def values(self, guild: discord.Guild) -> dict[str, str]:
        async with SessionLocal() as db:
            live = (await db.execute(select(Streamer.id).where(Streamer.guild_id == guild.id, Streamer.is_live.is_(True)).limit(1))).first()
        online = sum(1 for m in guild.members if m.status != discord.Status.offline and not m.bot) if settings.enable_presence_intent else 0
        return {"members": fmt_num(guild.member_count or 0), "online": fmt_num(online), "boosts": str(guild.premium_subscription_count or 0),
                "stream": "LIVE 🔴" if live else "Offline"}

    async def update_guild(self, guild: discord.Guild) -> None:
        cfg = await config.get(guild.id, "stats_channels")
        vals = await self.values(guild)
        for key in ("members", "online", "boosts", "stream"):
            ch = guild.get_channel(cfg.id(f"{key}_channel") or 0)
            if ch is None:
                continue
            name = fill(cfg.get(f"{key}_template") or "", **vals)[:95]
            if name and ch.name != name:
                try:
                    await asyncio.wait_for(ch.edit(name=name, reason="Stats-Channel"), timeout=10)
                except (discord.HTTPException, asyncio.TimeoutError):
                    pass

    async def create_channels(self, guild: discord.Guild) -> dict[str, str]:
        """Legt Kategorie + gesperrte Voice-Channels an und speichert sie in den Einstellungen."""
        cfg = await config.get(guild.id, "stats_channels")
        overwrites = {guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=True),
                      guild.me: discord.PermissionOverwrite(connect=True, manage_channels=True, view_channel=True)}
        category = await guild.create_category("📊 Server Stats", overwrites=overwrites, position=0, reason="Stats-Channels")
        vals = await self.values(guild)
        data = dict(cfg)
        for key in ("members", "online", "boosts", "stream"):
            ch = await guild.create_voice_channel(fill(cfg.get(f"{key}_template") or key, **vals)[:95], category=category, reason="Stats-Channels")
            data[f"{key}_channel"] = str(ch.id)
        await config.save(guild.id, "stats_channels", settings=data, enabled=True)
        return data

    @tasks.loop(minutes=10)
    async def loop(self):
        for guild in self.bot.guilds:
            if await config.enabled(guild.id, "stats_channels"):
                await self.update_guild(guild)
                await asyncio.sleep(1)

    @loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(TempAction)
    await bot.add_cog(TempVoice(bot))
    await bot.add_cog(StatsChannels(bot))
