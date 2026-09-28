"""Welcome & Leave, Auto-Rollen, Auto-DM, Server-Boosts und Invite-Tracking."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.ui import reply
from app.core.embeds import hex_to_color, theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import fill, i18n
from app.core.metrics import metrics
from app.core.timeutil import ts
from app.db.base import session_scope
from app.db.models import Member, MemberBadge
from app.services import achievements, progression
from app.services.members import ensure_member, touch_leave
from app.services.notify import notify

log = logging.getLogger("nova.welcome")


class Welcome(commands.Cog):
    module = "welcome"
    help_category = "admin"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invites: dict[int, dict[str, int]] = {}

    # ── Invite-Tracking ──
    async def _snapshot(self, guild: discord.Guild) -> None:
        if not guild.me.guild_permissions.manage_guild:
            return
        try:
            self.invites[guild.id] = {i.code: i.uses or 0 for i in await guild.invites()}
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:
            await self._snapshot(g)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._snapshot(guild)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild:
            self.invites.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild:
            self.invites.get(invite.guild.id, {}).pop(invite.code, None)

    async def _find_inviter(self, guild: discord.Guild) -> int | None:
        before = self.invites.get(guild.id, {})
        if not guild.me.guild_permissions.manage_guild:
            return None
        try:
            current = await guild.invites()
        except discord.HTTPException:
            return None
        inviter = None
        for inv in current:
            if (inv.uses or 0) > before.get(inv.code, 0) and inv.inviter:
                inviter = inv.inviter.id
                break
        self.invites[guild.id] = {i.code: i.uses or 0 for i in current}
        return inviter

    # ── Join ──
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        metrics.incr(guild.id, "joins")
        inviter_id = await self._find_inviter(guild)
        async with session_scope() as s:
            row = await ensure_member(s, guild.id, member)
            row.left_at = None
            if inviter_id and inviter_id != member.id and row.invited_by is None:
                row.invited_by = inviter_id
                inviter = await ensure_member(s, guild.id, guild.get_member(inviter_id) or inviter_id)
                inviter.invites += 1
        bus.publish(guild.id, "member_join", {"id": str(member.id), "name": str(member), "avatar": member.display_avatar.url,
                                              "count": guild.member_count})
        await notify(guild, "member_join", user=member.mention, username=member.name, created=ts(member.created_at, "R"),
                     server=guild.name, count=guild.member_count)

        if not await config.enabled(guild.id, "welcome"):
            return
        cfg = await config.get(guild.id, "welcome")
        if member.bot:
            bot_roles = [r for r in (guild.get_role(i) for i in cfg.ids("bot_roles")) if r and r < guild.me.top_role]
            if bot_roles:
                try:
                    await member.add_roles(*bot_roles, reason="Auto-Rollen (Bots)")
                except discord.HTTPException:
                    pass
            return
        # Auto-Rollen (nicht bei aktivierter Verifizierung mit Unverified-Rolle → die verteilt das Verification-Modul)
        roles = [guild.get_role(r) for r in cfg.ids("auto_roles")]
        roles = [r for r in roles if r and r < guild.me.top_role]
        if roles:
            try:
                await member.add_roles(*roles, reason="Auto-Rollen")
            except discord.HTTPException as exc:
                log.warning("Auto-Rollen fehlgeschlagen: %s", exc)

        ph = dict(user=member.mention, username=member.name, server=guild.name, count=guild.member_count)
        await self.send_welcome(member)

        if cfg.get("dm_enabled") and cfg.get("dm_message"):
            try:
                th = await theme(guild)
                e = th.embed(guild.name, fill(cfg["dm_message"], **ph), icon=False)
                if guild.icon:
                    e.set_thumbnail(url=guild.icon.url)
                await member.send(embed=e)
            except discord.HTTPException:
                pass

    async def send_welcome(self, member: discord.Member, channel: discord.abc.Messageable | None = None) -> bool:
        """Sendet die Willkommensnachricht (auch für /welcome test und die Dashboard-Vorschau)."""
        guild = member.guild
        cfg = await config.get(guild.id, "welcome")
        channel = channel or guild.get_channel(cfg.id("channel") or 0)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False
        ph = dict(user=member.mention, username=member.name, server=guild.name, count=guild.member_count)
        text = fill(cfg.get("message") or "", **ph)
        try:
            if cfg.get("use_embed", True):
                th = await theme(guild)
                e = discord.Embed(title=fill(cfg.get("embed_title") or "", **ph) or None, description=text,
                                  colour=th.color("primary"), timestamp=discord.utils.utcnow())
                e.set_author(name=str(member), icon_url=member.display_avatar.url)
                e.set_thumbnail(url=member.display_avatar.with_size(256).url)
                if cfg.get("image_url"):
                    e.set_image(url=cfg["image_url"])
                await channel.send(content=member.mention, embed=e, allowed_mentions=discord.AllowedMentions(users=True))
            else:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))
            return True
        except discord.HTTPException as exc:
            log.warning("Willkommensnachricht fehlgeschlagen: %s", exc)
            return False

    # ── Commands: Welcome & Auto-Rollen direkt aus Discord einstellen ──
    welcome_group = app_commands.Group(name="welcome", description="Willkommensnachricht einstellen", guild_only=True,
                                       default_permissions=discord.Permissions(manage_guild=True), extras={"module": "core"})
    autorole_group = app_commands.Group(name="autorole", description="Rollen, die neue Mitglieder automatisch bekommen", guild_only=True,
                                        default_permissions=discord.Permissions(manage_roles=True), extras={"module": "core"})

    async def _patch(self, guild_id: int, **values) -> None:
        cfg = dict(await config.get(guild_id, "welcome"))
        cfg.update(values)
        await config.save(guild_id, "welcome", settings=cfg, enabled=True)

    async def _ok(self, interaction: discord.Interaction, key: str, **kw) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _(key, **kw)))

    @welcome_group.command(name="channel", description="In welchen Channel soll die Begrüßung?")
    async def welcome_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self._patch(interaction.guild_id, channel=str(channel.id))
        await self._ok(interaction, "welcome.channel_set", channel=channel.mention)

    @welcome_group.command(name="message", description="Text der Begrüßung – Platzhalter: {user} {username} {server} {count}")
    async def welcome_message(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 1500], title: app_commands.Range[str, 0, 200] | None = None,
                              image_url: str | None = None):
        values = {"message": text.replace("\\n", "\n")}
        if title is not None:
            values["embed_title"] = title
        if image_url is not None:
            values["image_url"] = image_url if image_url.startswith("http") else ""
        await self._patch(interaction.guild_id, **values)
        await self._ok(interaction, "welcome.message_set")

    @welcome_group.command(name="dm", description="Zusätzlich eine private Nachricht an neue Mitglieder schicken")
    async def welcome_dm(self, interaction: discord.Interaction, enabled: bool, text: app_commands.Range[str, 0, 1500] | None = None):
        values: dict = {"dm_enabled": enabled}
        if text:
            values["dm_message"] = text.replace("\\n", "\n")
        await self._patch(interaction.guild_id, **values)
        await self._ok(interaction, "welcome.dm_set", state="✅" if enabled else "❌")

    @welcome_group.command(name="leave", description="Abschiedsnachricht an/aus")
    async def welcome_leave(self, interaction: discord.Interaction, enabled: bool, channel: discord.TextChannel | None = None,
                            text: app_commands.Range[str, 0, 1500] | None = None):
        values: dict = {"leave_enabled": enabled}
        if channel:
            values["leave_channel"] = str(channel.id)
        if text:
            values["leave_message"] = text.replace("\\n", "\n")
        await self._patch(interaction.guild_id, **values)
        await self._ok(interaction, "welcome.leave_set", state="✅" if enabled else "❌")

    @welcome_group.command(name="test", description="Zeigt die Begrüßung so, als wärst du gerade beigetreten")
    async def welcome_test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        sent = await self.send_welcome(interaction.user)  # type: ignore[arg-type]
        if not sent:
            raise UserError("welcome.no_channel")
        await interaction.followup.send("✅", ephemeral=True)

    @autorole_group.command(name="add", description="Rolle, die jedes neue Mitglied automatisch bekommt")
    async def autorole_add(self, interaction: discord.Interaction, role: discord.Role, bots: bool = False):
        if role >= interaction.guild.me.top_role or role.managed or role.is_default():
            raise UserError("roles.role_unavailable")
        key = "bot_roles" if bots else "auto_roles"
        cfg = await config.get(interaction.guild_id, "welcome")
        ids = list(dict.fromkeys([*(cfg.get(key) or []), str(role.id)]))
        await self._patch(interaction.guild_id, **{key: ids})
        await self._ok(interaction, "welcome.autorole_added", role=role.mention, target="Bots" if bots else "Mitglieder")

    @autorole_group.command(name="remove", description="Auto-Rolle entfernen")
    async def autorole_remove(self, interaction: discord.Interaction, role: discord.Role):
        cfg = await config.get(interaction.guild_id, "welcome")
        await self._patch(interaction.guild_id, auto_roles=[r for r in cfg.get("auto_roles") or [] if r != str(role.id)],
                          bot_roles=[r for r in cfg.get("bot_roles") or [] if r != str(role.id)])
        await self._ok(interaction, "welcome.autorole_removed", role=role.mention)

    @autorole_group.command(name="list", description="Alle Auto-Rollen anzeigen")
    async def autorole_list(self, interaction: discord.Interaction):
        cfg = await config.get(interaction.guild_id, "welcome")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        humans = " ".join(f"<@&{r}>" for r in cfg.get("auto_roles") or []) or "—"
        bots = " ".join(f"<@&{r}>" for r in cfg.get("bot_roles") or []) or "—"
        e = th.embed(_("welcome.autorole_title"), icon=False)
        e.add_field(name=_("welcome.for_members"), value=humans, inline=False)
        e.add_field(name=_("welcome.for_bots"), value=bots, inline=False)
        await reply(interaction, e)

    # ── Leave ──
    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        user = payload.user
        metrics.incr(guild.id, "leaves")
        async with session_scope() as s:
            row = await s.get(Member, (guild.id, user.id))
            if row:
                touch_leave(row)
        bus.publish(guild.id, "member_leave", {"id": str(user.id), "name": str(user), "count": guild.member_count})
        await notify(guild, "member_leave", user=user.mention, username=user.name, server=guild.name, count=guild.member_count)
        if user.bot or not await config.enabled(guild.id, "welcome"):
            return
        cfg = await config.get(guild.id, "welcome")
        if not cfg.get("leave_enabled"):
            return
        channel = guild.get_channel(cfg.id("leave_channel") or cfg.id("channel") or 0)
        if isinstance(channel, discord.TextChannel):
            th = await theme(guild)
            e = th.embed(None, fill(cfg.get("leave_message") or "", user=user.mention, username=user.name, server=guild.name,
                                    count=guild.member_count), kind="info", icon=False)
            e.set_author(name=str(user), icon_url=user.display_avatar.url)
            try:
                await channel.send(embed=e)
            except discord.HTTPException:
                pass

    # ── Boosts ──
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since is not None or after.premium_since is None:
            return
        guild = after.guild
        metrics.incr(guild.id, "boosts")
        bus.publish(guild.id, "boost", {"id": str(after.id), "name": str(after)})
        await notify(guild, "boost", user=after.mention, username=after.name, server=guild.name)
        async with session_scope() as s:
            row = await ensure_member(s, guild.id, after)
        await achievements.check(self.bot, guild, row, after)
        if not await config.enabled(guild.id, "boost"):
            return
        cfg = await config.get(guild.id, "boost")
        role = guild.get_role(cfg.id("booster_role") or 0)
        if role and role < guild.me.top_role:
            try:
                await after.add_roles(role, reason="Server Boost")
            except discord.HTTPException:
                pass
        if cfg.get("reward_coins"):
            await progression.add_coins(guild.id, after, int(cfg["reward_coins"]), reason="boost")
        if cfg.get("reward_xp"):
            await progression.add_xp(self.bot, guild, after, int(cfg["reward_xp"]), use_multiplier=False)
        if cfg.id("reward_badge"):
            async with session_scope() as s:
                if not await s.get(MemberBadge, (guild.id, after.id, cfg.id("reward_badge"))):
                    s.add(MemberBadge(guild_id=guild.id, user_id=after.id, badge_id=cfg.id("reward_badge")))
        channel = guild.get_channel(cfg.id("channel") or 0)
        if isinstance(channel, discord.TextChannel):
            th = await theme(guild)
            e = discord.Embed(description=fill(cfg.get("message") or "", user=after.mention, username=after.name, server=guild.name),
                              colour=hex_to_color("#f47fff"), timestamp=discord.utils.utcnow())
            e.set_author(name=str(after), icon_url=after.display_avatar.url)
            e.set_thumbnail(url=after.display_avatar.url)
            e.set_footer(text=f"🚀 {guild.premium_subscription_count} Boosts · Level {guild.premium_tier}", icon_url=th.icon_url)
            try:
                await channel.send(content=after.mention, embed=e, allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
