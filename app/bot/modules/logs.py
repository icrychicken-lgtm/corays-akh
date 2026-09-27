"""Server-Logging: Nachrichten, Member, Bans/Kicks/Timeouts, Rollen, Channels, Voice, Nicknames.
Manuelle Moderationsaktionen (über die Discord-App) werden ebenfalls als Case erfasst."""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from app.core.embeds import theme
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import log_event
from app.core.timeutil import ts
from app.services import moderation as mod
from app.services.notify import send_log

log = logging.getLogger("nova.logs")


class Logs(commands.Cog):
    module = "logging"
    help_category = "admin"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _active(self, guild: discord.Guild | None, event: str, channel_id: int | None = None) -> bool:
        if guild is None or not await config.enabled(guild.id, "logging"):
            return False
        cfg = await config.get(guild.id, "logging")
        if event not in (cfg.get("events") or []):
            return False
        if channel_id and channel_id in cfg.ids("ignored_channels"):
            return False
        return True

    async def _emit(self, guild: discord.Guild, kind: str, event: str, title_key: str, desc: str, *, color: str = "info",
                    user: discord.abc.User | None = None, fields: list[tuple[str, str]] | None = None,
                    db_category: str, db_action: str, channel_id: int | None = None, target_id: int | None = None,
                    content: str = "", details: dict | None = None) -> None:
        await log_event(guild.id, db_category, db_action, user=user, channel_id=channel_id, target_id=target_id,
                        content=content, details=details)
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = th.embed(_(title_key), desc[:4000], kind=color, user=user)
        for name, value in fields or []:
            e.add_field(name=_(name), value=(value or "—")[:1024], inline=False)
        if user is not None:
            e.set_footer(text=f"User-ID {user.id}", icon_url=th.icon_url)
        await send_log(guild, kind, e)

    # ── Nachrichten ──
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or not await self._active(message.guild, "message_delete", message.channel.id):
            return
        cfg = await config.get(message.guild.id, "logging")
        if message.author.bot and cfg.get("ignore_bots", True):
            return
        att = "\n".join(a.url for a in message.attachments)
        await self._emit(message.guild, "message", "message_delete", "logs.message_delete",
                         f"{message.author.mention} · {message.channel.mention}", color="error", user=message.author,
                         fields=[("logs.f_content", message.content), ("logs.f_attachments", att)] if att else [("logs.f_content", message.content)],
                         db_category="message", db_action="delete", channel_id=message.channel.id,
                         content=message.content, details={"attachments": [a.url for a in message.attachments]})

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages or messages[0].guild is None:
            return
        first = messages[0]
        if not await self._active(first.guild, "bulk_delete", first.channel.id):
            return
        await self._emit(first.guild, "message", "bulk_delete", "logs.bulk_delete",
                         f"**{len(messages)}** · {first.channel.mention}", color="error",
                         db_category="message", db_action="bulk_delete", channel_id=first.channel.id, content=str(len(messages)))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild is None or before.content == after.content or after.author.bot:
            return
        if not await self._active(after.guild, "message_edit", after.channel.id):
            return
        await self._emit(after.guild, "message", "message_edit", "logs.message_edit",
                         f"{after.author.mention} · {after.channel.mention} · [Jump]({after.jump_url})", color="warning", user=after.author,
                         fields=[("logs.f_before", before.content), ("logs.f_after", after.content)],
                         db_category="message", db_action="edit", channel_id=after.channel.id,
                         content=after.content, details={"before": before.content[:1500]})

    # ── Member ──
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not await self._active(member.guild, "member_join"):
            return
        await self._emit(member.guild, "member", "member_join", "logs.member_join",
                         f"{member.mention} · {member}\n📅 {ts(member.created_at, 'R')}", color="success", user=member,
                         db_category="member", db_action="join", target_id=member.id,
                         details={"created": member.created_at.isoformat(), "count": member.guild.member_count})

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or not await self._active(guild, "member_leave"):
            return
        user = payload.user
        roles = ", ".join(r.mention for r in getattr(user, "roles", [])[1:]) if isinstance(user, discord.Member) else ""
        await self._emit(guild, "member", "member_leave", "logs.member_leave", f"{user.mention} · {user}", color="error", user=user,
                         fields=[("logs.f_roles", roles)] if roles else None, db_category="member", db_action="leave", target_id=user.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        g = after.guild
        if before.nick != after.nick and await self._active(g, "nickname"):
            await self._emit(g, "member", "nickname", "logs.nickname", after.mention, user=after,
                             fields=[("logs.f_before", before.nick or before.name), ("logs.f_after", after.nick or after.name)],
                             db_category="member", db_action="nickname", target_id=after.id,
                             content=f"{before.nick or before.name} → {after.nick or after.name}")
        if before.roles != after.roles and await self._active(g, "role_change"):
            added = [r for r in after.roles if r not in before.roles]
            removed = [r for r in before.roles if r not in after.roles]
            desc = after.mention
            fields = []
            if added:
                fields.append(("logs.f_added", " ".join(r.mention for r in added)))
            if removed:
                fields.append(("logs.f_removed", " ".join(r.mention for r in removed)))
            await self._emit(g, "member", "role_change", "logs.role_change", desc, user=after, fields=fields,
                             db_category="role", db_action="member_roles", target_id=after.id,
                             content=" ".join([f"+{r.name}" for r in added] + [f"-{r.name}" for r in removed]))

    # ── Audit-Log: manuelle Bans/Kicks/Timeouts als Case erfassen ──
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        if self.bot.user and entry.user_id == self.bot.user.id:
            return  # eigene Aktionen sind bereits erfasst
        if not await config.enabled(guild.id, "moderation"):
            return
        action = None
        duration = None
        A = discord.AuditLogAction
        if entry.action == A.ban:
            action = "ban"
        elif entry.action == A.unban:
            action = "unban"
        elif entry.action == A.kick:
            action = "kick"
        elif entry.action == A.member_update:
            before = getattr(entry.before, "timed_out_until", None)
            after = getattr(entry.after, "timed_out_until", None)
            if after and after != before:
                action = "timeout"
                duration = int((after - entry.created_at).total_seconds())
            elif before and not after:
                action = "untimeout"
        if not action or entry.target is None:
            return
        target = entry.target if isinstance(entry.target, (discord.User, discord.Member)) else await self.bot.fetch_user(entry.target.id)
        moderator = entry.user or guild.me
        case = await mod.create_case(guild, action, target, moderator, entry.reason or "", duration, source="discord",
                                     active=action in ("timeout", "ban"))
        await mod.send_modlog(guild, await mod.case_embed(guild, case, target))
        if await self._active(guild, {"ban": "member_ban", "unban": "member_unban", "kick": "member_kick"}.get(action, "member_timeout")):
            await log_event(guild.id, "moderation", action, user=moderator, target_id=target.id, content=entry.reason or "",
                            details={"case": case.case_number, "source": "discord"})

    # ── Rollen & Channels ──
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if await self._active(role.guild, "role_update"):
            await self._emit(role.guild, "server", "role_update", "logs.role_create", f"{role.mention} · `{role.name}`", color="success",
                             db_category="role", db_action="create", target_id=role.id, content=role.name)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if await self._active(role.guild, "role_update"):
            await self._emit(role.guild, "server", "role_update", "logs.role_delete", f"`{role.name}`", color="error",
                             db_category="role", db_action="delete", target_id=role.id, content=role.name)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if not await self._active(after.guild, "role_update"):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"Farbe: `{before.color}` → `{after.color}`")
        if before.permissions != after.permissions:
            added = [p for p, v in after.permissions if v and not getattr(before.permissions, p)]
            removed = [p for p, v in before.permissions if v and not getattr(after.permissions, p)]
            if added:
                changes.append("➕ " + ", ".join(added))
            if removed:
                changes.append("➖ " + ", ".join(removed))
        if before.hoist != after.hoist:
            changes.append(f"Hoist: {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"Mentionable: {after.mentionable}")
        if not changes:
            return
        await self._emit(after.guild, "server", "role_update", "logs.role_update", f"{after.mention}\n" + "\n".join(changes), color="warning",
                         db_category="role", db_action="update", target_id=after.id, content="; ".join(changes))

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if await self._active(channel.guild, "channel_update"):
            await self._emit(channel.guild, "server", "channel_update", "logs.channel_create", f"{channel.mention} · `{channel.name}`", color="success",
                             db_category="channel", db_action="create", channel_id=channel.id, content=channel.name)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if await self._active(channel.guild, "channel_update"):
            await self._emit(channel.guild, "server", "channel_update", "logs.channel_delete", f"`#{channel.name}`", color="error",
                             db_category="channel", db_action="delete", channel_id=channel.id, content=channel.name)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if not await self._active(after.guild, "channel_update", after.id):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append("Topic geändert")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(f"Slowmode: {getattr(after, 'slowmode_delay', 0)}s")
        if before.overwrites != after.overwrites:
            changes.append("Berechtigungen geändert")
        if not changes:
            return
        await self._emit(after.guild, "server", "channel_update", "logs.channel_update", f"{after.mention}\n" + "\n".join(changes), color="warning",
                         db_category="channel", db_action="update", channel_id=after.id, content="; ".join(changes))

    # ── Voice ──
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel or not await self._active(member.guild, "voice"):
            return
        if before.channel is None:
            key, action, desc, color = "logs.voice_join", "join", f"{member.mention} → 🔊 {after.channel.mention}", "success"
        elif after.channel is None:
            key, action, desc, color = "logs.voice_leave", "leave", f"{member.mention} ← 🔊 {before.channel.mention}", "error"
        else:
            key, action, desc, color = "logs.voice_move", "move", f"{member.mention}: {before.channel.mention} → {after.channel.mention}", "info"
        await self._emit(member.guild, "voice", "voice", key, desc, color=color, user=member,
                         db_category="voice", db_action=action, channel_id=(after.channel or before.channel).id,
                         content=(after.channel or before.channel).name)


async def setup(bot: commands.Bot):
    await bot.add_cog(Logs(bot))
