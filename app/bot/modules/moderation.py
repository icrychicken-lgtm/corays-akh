"""Moderation: Warn, Timeout, Kick, Ban, Softban, Purge, Slowmode, Lock, Nick, Cases, Historie."""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select

from app.bot.ui import BaseView, Paginator, chunk, confirm, fail, guarded, reply
from app.core.embeds import theme
from app.core.errors import UserError
from app.core.i18n import i18n
from app.core.records import log_event
from app.core.timeutil import human_duration, parse_duration, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import ModCase
from app.services import moderation as mod
from app.services.notify import is_staff

log = logging.getLogger("nova.mod")
DURATIONS = ["10m", "30m", "1h", "6h", "12h", "1d", "3d", "7d", "14d", "28d"]


async def _duration_ac(_i: discord.Interaction, current: str):
    return [app_commands.Choice(name=d, value=d) for d in DURATIONS if current.lower() in d][:25]


async def _reason_ac(interaction: discord.Interaction, current: str):
    from app.bot.modules.modtools import reason_choices
    return await reason_choices(interaction, current)


def _parse(text: str | None, allow_none: bool = True) -> int | None:
    if not text:
        if allow_none:
            return None
        raise UserError("mod.err_duration")
    secs = parse_duration(text)
    if secs is None:
        raise UserError("mod.err_duration")
    return secs


class CaseReasonModal(discord.ui.Modal):
    def __init__(self, case_id: int, title: str, label: str, current: str):
        super().__init__(title=title)
        self.case_id = case_id
        self.reason = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph, default=current[:1000], max_length=1000)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        async with session_scope() as s:
            case = await s.get(ModCase, self.case_id)
            if case is None or case.guild_id != interaction.guild_id:
                raise UserError("mod.case_not_found")
            case.reason = self.reason.value
            case.updated_at = utcnow()
        e = await mod.case_embed(interaction.guild, case)
        await interaction.response.edit_message(embed=e)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        from app.bot.ui import handle_exception
        await handle_exception(interaction, error)


class CaseView(BaseView):
    def __init__(self, case: ModCase, owner_id: int, labels: dict[str, str]):
        super().__init__(owner_id=owner_id, timeout=300)
        self.case = case
        self.labels = labels
        self.edit_reason.label = labels["edit"]
        self.toggle_active.label = labels["deactivate"] if case.active else labels["activate"]
        if case.action not in ("warn", "timeout", "ban"):
            self.remove_item(self.toggle_active)

    @discord.ui.button(emoji="✏️", style=discord.ButtonStyle.secondary)
    @guarded
    async def edit_reason(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(CaseReasonModal(self.case.id, self.labels["modal_title"], self.labels["reason"], self.case.reason))

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    @guarded
    async def toggle_active(self, interaction: discord.Interaction, _b: discord.ui.Button):
        async with session_scope() as s:
            case = await s.get(ModCase, self.case.id)
            case.active = not case.active
            case.updated_at = utcnow()
            self.case = case
        self.toggle_active.label = self.labels["deactivate"] if self.case.active else self.labels["activate"]
        await interaction.response.edit_message(embed=await mod.case_embed(interaction.guild, self.case), view=self)


class WarnModal(discord.ui.Modal):
    def __init__(self, target: discord.Member, title: str, label: str):
        super().__init__(title=title)
        self.target = target
        self.reason = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph, max_length=500, required=False)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        case = await mod.execute(interaction.guild, "warn", self.target, interaction.user, self.reason.value)
        await interaction.response.send_message(embed=await mod.case_embed(interaction.guild, case, self.target), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        from app.bot.ui import handle_exception
        await handle_exception(interaction, error)


@app_commands.guild_only()
class Moderation(commands.Cog):
    module = "moderation"
    help_category = "moderation"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expire_loop.start()

    def cog_unload(self):
        self.expire_loop.cancel()

    async def _done(self, interaction: discord.Interaction, case: ModCase, target: discord.abc.User) -> None:
        await reply(interaction, await mod.case_embed(interaction.guild, case, target), ephemeral=True)

    # ── Commands ──
    @app_commands.autocomplete(reason=_reason_ac)
    @app_commands.command(name="warn", description="Verwarnt einen User")
    @app_commands.default_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 0, 500] = ""):
        await interaction.response.defer(ephemeral=True)
        case = await mod.execute(interaction.guild, "warn", user, interaction.user, reason)
        await self._done(interaction, case, user)

    @app_commands.autocomplete(reason=_reason_ac)
    @app_commands.command(name="timeout", description="Versetzt einen User in Timeout")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(duration="z. B. 10m, 2h, 1d (max. 28d)")
    @app_commands.autocomplete(duration=_duration_ac)
    async def timeout(self, interaction: discord.Interaction, user: discord.Member, duration: str = "", reason: app_commands.Range[str, 0, 500] = ""):
        secs = _parse(duration)
        await interaction.response.defer(ephemeral=True)
        case = await mod.execute(interaction.guild, "timeout", user, interaction.user, reason, duration=secs)
        await self._done(interaction, case, user)

    @app_commands.command(name="untimeout", description="Hebt einen Timeout auf")
    @app_commands.default_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 0, 500] = ""):
        if not user.is_timed_out():
            raise UserError("mod.err_not_timed_out")
        await interaction.response.defer(ephemeral=True)
        case = await mod.execute(interaction.guild, "untimeout", user, interaction.user, reason)
        await self._done(interaction, case, user)

    @app_commands.autocomplete(reason=_reason_ac)
    @app_commands.command(name="kick", description="Kickt einen User")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 0, 500] = ""):
        _ = await i18n.for_guild(interaction.guild_id)
        if not await confirm(interaction, _("mod.confirm_title"), _("mod.confirm_kick", user=user.mention)):
            return
        case = await mod.execute(interaction.guild, "kick", user, interaction.user, reason)
        await self._done(interaction, case, user)

    @app_commands.autocomplete(reason=_reason_ac)
    @app_commands.command(name="ban", description="Bannt einen User (auch per ID)")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.describe(duration="Optional: temporärer Ban, z. B. 7d", delete_days="Nachrichten der letzten X Tage löschen")
    @app_commands.autocomplete(duration=_duration_ac)
    async def ban(self, interaction: discord.Interaction, user: discord.User, reason: app_commands.Range[str, 0, 500] = "",
                  duration: str = "", delete_days: app_commands.Range[int, 0, 7] = 0):
        _ = await i18n.for_guild(interaction.guild_id)
        secs = _parse(duration)
        if not await confirm(interaction, _("mod.confirm_title"), _("mod.confirm_ban", user=user.mention)):
            return
        case = await mod.execute(interaction.guild, "ban", user, interaction.user, reason, duration=secs, delete_days=delete_days)
        await self._done(interaction, case, user)

    @app_commands.command(name="unban", description="Entbannt einen User per ID")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: app_commands.Range[str, 0, 500] = ""):
        if not user_id.isdigit():
            raise UserError("mod.err_invalid_id")
        await interaction.response.defer(ephemeral=True)
        try:
            user = await self.bot.fetch_user(int(user_id))
        except discord.NotFound:
            raise UserError("mod.err_not_found")
        case = await mod.execute(interaction.guild, "unban", user, interaction.user, reason)
        await self._done(interaction, case, user)

    @unban.autocomplete("user_id")
    async def _unban_ac(self, interaction: discord.Interaction, current: str):
        out = []
        try:
            async for entry in interaction.guild.bans(limit=500):
                label = f"{entry.user} ({entry.user.id})"
                if current.lower() in label.lower():
                    out.append(app_commands.Choice(name=label[:100], value=str(entry.user.id)))
                if len(out) >= 25:
                    break
        except discord.HTTPException:
            pass
        return out

    @app_commands.command(name="softban", description="Bannt und entbannt sofort (löscht Nachrichten)")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def softban(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 0, 500] = "",
                      delete_days: app_commands.Range[int, 1, 7] = 1):
        _ = await i18n.for_guild(interaction.guild_id)
        if not await confirm(interaction, _("mod.confirm_title"), _("mod.confirm_softban", user=user.mention)):
            return
        case = await mod.execute(interaction.guild, "softban", user, interaction.user, reason, delete_days=delete_days)
        await self._done(interaction, case, user)

    @app_commands.command(name="purge", description="Löscht mehrere Nachrichten")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @app_commands.describe(amount="Anzahl (1–500)", user="Nur Nachrichten dieses Users", contains="Nur Nachrichten mit diesem Text", bots="Nur Bot-Nachrichten")
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500], user: discord.User | None = None,
                    contains: str | None = None, bots: bool = False):
        _ = await i18n.for_guild(interaction.guild_id)
        if amount > 25 and not await confirm(interaction, _("mod.confirm_title"), _("mod.confirm_purge", amount=amount)):
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        def check(m: discord.Message) -> bool:
            if m.pinned:
                return False
            if user and m.author.id != user.id:
                return False
            if bots and not m.author.bot:
                return False
            if contains and contains.lower() not in m.content.lower():
                return False
            return True

        deleted = await interaction.channel.purge(limit=amount, check=check, before=discord.Object(interaction.id), reason=f"Purge by {interaction.user}")
        await log_event(interaction.guild_id, "moderation", "purge", user=interaction.user, channel_id=interaction.channel_id,
                        content=f"{len(deleted)} Nachrichten", details={"filter_user": str(user) if user else None})
        th = await theme(interaction.guild)
        await interaction.followup.send(embed=th.success(_("common.success"), _("mod.purged", count=len(deleted))), ephemeral=True)

    @app_commands.command(name="slowmode", description="Setzt den Slowmode eines Channels")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(duration="z. B. 5s, 1m, 1h – 0 zum Deaktivieren")
    async def slowmode(self, interaction: discord.Interaction, duration: str, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        raw = duration.strip().lower()
        secs = 0 if raw in ("0", "off", "aus") else int(raw) if raw.isdigit() else parse_duration(raw)
        if secs is None or secs > 21600:
            raise UserError("mod.err_slowmode")
        await channel.edit(slowmode_delay=secs, reason=f"Slowmode by {interaction.user}")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        msg = _("mod.slowmode_off", channel=channel.mention) if secs == 0 else _("mod.slowmode_set", channel=channel.mention, duration=human_duration(secs))
        await reply(interaction, th.success(_("common.success"), msg))

    async def set_lock(self, channel: discord.TextChannel, locked: bool, reason: str) -> None:
        overwrite = channel.overwrites_for(channel.guild.default_role)
        overwrite.send_messages = False if locked else None
        overwrite.add_reactions = False if locked else None
        overwrite.create_public_threads = False if locked else None
        await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason=reason)

    @app_commands.command(name="lock", description="Sperrt einen Channel für @everyone")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None, reason: str = ""):
        channel = channel or interaction.channel
        await self.set_lock(channel, True, f"Lock by {interaction.user}: {reason}"[:500])
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await log_event(interaction.guild_id, "moderation", "lock", user=interaction.user, channel_id=channel.id, content=reason)
        await channel.send(embed=th.warning(_("mod.locked_title"), _("mod.locked_desc", reason=reason or _("mod.no_reason"))))
        await reply(interaction, th.success(_("common.success"), _("mod.locked", channel=channel.mention)))

    @app_commands.command(name="unlock", description="Entsperrt einen Channel")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        await self.set_lock(channel, False, f"Unlock by {interaction.user}")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await log_event(interaction.guild_id, "moderation", "unlock", user=interaction.user, channel_id=channel.id)
        await channel.send(embed=th.success(_("mod.unlocked_title"), _("mod.unlocked_desc")))
        await reply(interaction, th.success(_("common.success"), _("mod.unlocked", channel=channel.mention)))

    @app_commands.command(name="nick", description="Ändert den Nickname eines Users")
    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.checks.bot_has_permissions(manage_nicknames=True)
    @app_commands.describe(nickname="Leer lassen zum Zurücksetzen")
    async def nick(self, interaction: discord.Interaction, user: discord.Member, nickname: app_commands.Range[str, 0, 32] = ""):
        if user.id != interaction.user.id:
            mod.check_hierarchy(interaction.guild, interaction.user, user)
        old = user.display_name
        await user.edit(nick=nickname or None, reason=f"Nick by {interaction.user}")
        await log_event(interaction.guild_id, "moderation", "nick", user=interaction.user, target_id=user.id, content=f"{old} → {nickname or user.name}")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await reply(interaction, th.success(_("common.success"), _("mod.nick_changed", user=user.mention, nick=nickname or user.name)))

    @app_commands.command(name="warnlist", description="Zeigt die aktiven Verwarnungen eines Users")
    @app_commands.default_permissions(moderate_members=True)
    async def warnlist(self, interaction: discord.Interaction, user: discord.User):
        await self._history(interaction, user, actions=["warn"])

    @app_commands.command(name="modhistory", description="Komplette Moderationshistorie eines Users")
    @app_commands.default_permissions(moderate_members=True)
    async def modhistory(self, interaction: discord.Interaction, user: discord.User):
        await self._history(interaction, user)

    async def _history(self, interaction: discord.Interaction, user: discord.abc.User, actions: list[str] | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        lang = await i18n.lang(interaction.guild_id)
        th = await theme(interaction.guild)
        async with SessionLocal() as s:
            q = select(ModCase).where(ModCase.guild_id == interaction.guild_id, ModCase.user_id == user.id)
            if actions:
                q = q.where(ModCase.action.in_(actions), ModCase.active.is_(True))
            cases = (await s.execute(q.order_by(ModCase.case_number.desc()).limit(200))).scalars().all()
        title = _("mod.warnlist_title" if actions else "mod.history_title", user=user.display_name)
        if not cases:
            await reply(interaction, th.info(title, _("mod.history_empty")))
            return
        pages = []
        for group in chunk(cases, 8):
            e = th.embed(title, user=user, icon=False)
            e.set_thumbnail(url=user.display_avatar.url)
            for c in group:
                dur = f" · {human_duration(c.duration_seconds, lang)}" if c.duration_seconds else ""
                state = "" if c.active or c.action not in ("warn", "timeout", "ban") else " · ~~inaktiv~~"
                e.add_field(
                    name=f"{mod.ACTION_ICONS.get(c.action, '•')} #{c.case_number} · {_('mod.action.' + c.action)}{dur}{state}",
                    value=f"{(c.reason or _('mod.no_reason'))[:200]}\n╰ <@{c.moderator_id}> · {ts(c.created_at, 'R')}", inline=False)
            e.description = _("mod.history_count", count=len(cases))
            pages.append(e)
        await Paginator(interaction.user.id, pages).send(interaction, ephemeral=True)

    @app_commands.command(name="case", description="Zeigt und bearbeitet einen Moderations-Case")
    @app_commands.default_permissions(moderate_members=True)
    async def case(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 10_000_000]):
        _ = await i18n.for_guild(interaction.guild_id)
        async with SessionLocal() as s:
            c = (await s.execute(select(ModCase).where(ModCase.guild_id == interaction.guild_id, ModCase.case_number == number))).scalar_one_or_none()
        if c is None:
            raise UserError("mod.case_not_found")
        labels = {"edit": _("mod.edit_reason"), "deactivate": _("mod.deactivate"), "activate": _("mod.activate"),
                  "modal_title": _("mod.case_title", number=number), "reason": _("mod.f_reason")}
        view = CaseView(c, interaction.user.id, labels)
        await reply(interaction, await mod.case_embed(interaction.guild, c), view=view)
        view.message = await interaction.original_response()

    # ── Hintergrund: Temp-Bans aufheben, abgelaufene Timeouts deaktivieren ──
    @tasks.loop(seconds=60)
    async def expire_loop(self):
        now = utcnow()
        async with SessionLocal() as s:
            rows = (await s.execute(select(ModCase).where(
                ModCase.active.is_(True), ModCase.expires_at.is_not(None), ModCase.expires_at <= now,
                ModCase.action.in_(["ban", "timeout"])).limit(100))).scalars().all()
        for c in rows:
            guild = self.bot.get_guild(c.guild_id)
            if c.action == "ban" and guild is not None:
                try:
                    user = await self.bot.fetch_user(c.user_id)
                    await mod.execute(guild, "unban", user, guild.me, f"Temp-Ban abgelaufen (Case #{c.case_number})", source="auto")
                except (mod.ModerationError, discord.HTTPException) as exc:
                    log.info("Temp-Ban %s konnte nicht aufgehoben werden: %s", c.id, exc)
            async with session_scope() as s:
                row = await s.get(ModCase, c.id)
                if row:
                    row.active = False if row.action == "timeout" or row.action == "ban" else row.active
                    row.updated_at = now

    @expire_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    cog = Moderation(bot)
    await bot.add_cog(cog)

    @app_commands.context_menu(name="Verwarnen")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def ctx_warn(interaction: discord.Interaction, user: discord.Member):
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.send_modal(WarnModal(user, _("mod.warn_modal_title", user=user.display_name)[:45], _("mod.f_reason")))

    @app_commands.context_menu(name="Nachricht melden")
    @app_commands.guild_only()
    async def ctx_report(interaction: discord.Interaction, message: discord.Message):
        from app.bot.ui import handle_exception
        try:
            await bot.get_cog("ModTools").send_report(interaction, message)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "report")

    for cm in (ctx_warn, ctx_report):
        cm.extras["module"] = "moderation"
        cm.extras["help_category"] = "moderation"
        bot.tree.add_command(cm)
