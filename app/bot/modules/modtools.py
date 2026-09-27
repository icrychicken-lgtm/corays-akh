"""Mod-Tools: Mod-Akte, Notizen, Quarantäne, Watchlist, Snipe, Dienst-Modus, Mod-Ruf, Mod-Statistik, Server-Lockdown,
Massban, Voice-Tools, neue Mitglieder, Grund-Vorlagen und Report-Buttons (Übernehmen, Löschen, Verwarnen, Erledigt)."""
from __future__ import annotations

import re
import time
from collections import Counter, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from app.bot.ui import confirm, handle_exception, reply
from app.core.embeds import theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import log_event
from app.core.timeutil import ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import ModCase, UserNote
from app.services import moderation as mod
from app.services.notify import is_staff

SNIPE_KEEP = 60 * 60       # gelöschte Nachrichten 1 Stunde im Speicher (nur RAM)
NEW_ACCOUNT_DAYS = 7


async def require_staff(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.user, discord.Member) or not await is_staff(interaction.user):
        raise UserError("errors.no_permission")


async def mod_channel(guild: discord.Guild, key: str | None = None) -> discord.TextChannel | None:
    cfg = await config.get(guild.id, "moderation")
    for cid in (cfg.id(key) if key else None, cfg.id("log_channel")):
        ch = guild.get_channel(cid or 0)
        if isinstance(ch, discord.TextChannel):
            return ch
    return None


async def reason_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Grund-Vorlagen aus dem Dashboard – für /warn, /timeout, /kick, /ban."""
    cfg = await config.get(interaction.guild_id, "moderation")
    out = [app_commands.Choice(name=r[:100], value=r[:500]) for r in cfg.get("reason_templates") or [] if current.lower() in r.lower()]
    if current and all(c.value != current for c in out):
        out.insert(0, app_commands.Choice(name=current[:100], value=current[:500]))
    return out[:25]


def account_flags(_, member: discord.Member | discord.User) -> list[str]:
    flags = []
    age = (utcnow() - member.created_at).days
    if age < NEW_ACCOUNT_DAYS:
        flags.append(_("mt.flag_new", days=age))
    if member.avatar is None:
        flags.append(_("mt.flag_no_avatar"))
    if isinstance(member, discord.Member) and member.is_timed_out():
        flags.append(_("mt.flag_timeout"))
    return flags


# ───────────────────────── Report-Buttons ─────────────────────────
class ReportAction(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"nova:rp:(?P<act>claim|del|warn|done):(?P<ch>\d+):(?P<msg>\d+):(?P<user>\d+)"):
    STYLE = {"claim": (discord.ButtonStyle.primary, "🙋"), "del": (discord.ButtonStyle.secondary, "🗑️"),
             "warn": (discord.ButtonStyle.secondary, "⚠️"), "done": (discord.ButtonStyle.success, "✅")}

    def __init__(self, act: str, ch: int, msg: int, user: int, label: str | None = None, disabled: bool = False):
        style, emoji = self.STYLE[act]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled, custom_id=f"nova:rp:{act}:{ch}:{msg}:{user}"))
        self.act, self.ch, self.msg, self.user = act, ch, msg, user

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["act"], int(match["ch"]), int(match["msg"]), int(match["user"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            await require_staff(interaction)
            tools: ModTools = interaction.client.get_cog("ModTools")  # type: ignore[assignment]
            await tools.report_action(interaction, self.act, self.ch, self.msg, self.user)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "report")


def report_view(_, ch: int, msg: int, user: int, done: bool = False) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    for act in ("claim", "del", "warn", "done"):
        v.add_item(ReportAction(act, ch, msg, user, _("mt.rp_" + act), disabled=done))
    return v


class NoteModal(discord.ui.Modal):
    def __init__(self, target: discord.abc.User, _):
        super().__init__(title=_("mt.note_modal", user=target.display_name)[:45])
        self.target = target
        self.text = discord.ui.TextInput(label=_("mt.note_label")[:45], style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            tools: ModTools = interaction.client.get_cog("ModTools")  # type: ignore[assignment]
            await tools.add_note(interaction, self.target, self.text.value)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "note")


class DossierView(discord.ui.View):
    """Schnellaktionen unter der Mod-Akte."""

    def __init__(self, target: discord.Member | discord.User, _):
        super().__init__(timeout=600)
        self.target, self._ = target, _
        self.note.label, self.timeout10.label, self.warn.label = _("mt.btn_note"), _("mt.btn_timeout"), _("mt.btn_warn")
        if not isinstance(target, discord.Member):
            self.remove_item(self.timeout10)
            self.remove_item(self.warn)

    @discord.ui.button(emoji="📝", style=discord.ButtonStyle.secondary)
    async def note(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.send_modal(NoteModal(self.target, self._))

    @discord.ui.button(emoji="⏳", style=discord.ButtonStyle.secondary)
    async def timeout10(self, interaction: discord.Interaction, _b: discord.ui.Button):
        try:
            await require_staff(interaction)
            case = await mod.execute(interaction.guild, "timeout", self.target, interaction.user, self._("mt.quick_reason"), duration=600)
            await reply(interaction, await mod.case_embed(interaction.guild, case, self.target))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "dossier")

    @discord.ui.button(emoji="⚠️", style=discord.ButtonStyle.secondary)
    async def warn(self, interaction: discord.Interaction, _b: discord.ui.Button):
        from app.bot.modules.moderation import WarnModal
        await interaction.response.send_modal(WarnModal(self.target, self._("mod.warn_modal_title", user=self.target.display_name)[:45],
                                                        self._("mod.f_reason")))


class ClaimCall(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:mc:(?P<user>\d+)"):
    def __init__(self, user: int, label: str | None = None):
        super().__init__(discord.ui.Button(label=label, emoji="🙋", style=discord.ButtonStyle.primary, custom_id=f"nova:mc:{user}"))
        self.user = user

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["user"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            await require_staff(interaction)
            _ = await i18n.for_guild(interaction.guild_id)
            e = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
            e.colour = discord.Colour.green()
            e.add_field(name=_("mt.call_claimed"), value=f"{interaction.user.mention} · {ts(utcnow(), 'R')}", inline=False)
            await interaction.response.edit_message(embed=e, view=None)
            try:
                member = interaction.guild.get_member(self.user)
                if member:
                    await member.send(embed=(await theme(interaction.guild)).success(_("mt.call_dm_title"), _("mt.call_dm", mod=interaction.user.display_name)))
            except discord.HTTPException:
                pass
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "modcall")


# ───────────────────────── Cog ─────────────────────────
class ModTools(commands.Cog, name="ModTools"):
    module = "moderation"
    help_category = "moderation"

    m = app_commands.Group(name="mod", description="Mod-Tools", guild_only=True, default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._snipes: dict[int, deque] = {}
        self._calls: dict[tuple[int, int], float] = {}

    # ── Listener ──
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot or not (message.content or message.attachments):
            return
        q = self._snipes.setdefault(message.channel.id, deque(maxlen=10))
        q.append((time.time(), message.author.id, str(message.author), message.content[:1000], [a.filename for a in message.attachments]))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        watch = await config.state(f"mt:watch:{message.guild.id}") or {}
        if str(message.author.id) not in watch:
            return
        ch = await mod_channel(message.guild, "watch_channel")
        if ch is None or ch.id == message.channel.id:
            return
        _ = await i18n.for_guild(message.guild.id)
        e = discord.Embed(description=(message.content or "*—*")[:3000], colour=discord.Colour.orange(), timestamp=message.created_at)
        e.set_author(name=f"👁️ {message.author} · #{message.channel}", icon_url=message.author.display_avatar.url)
        if message.attachments:
            e.add_field(name=_("mt.attachments"), value="\n".join(a.filename for a in message.attachments)[:1000], inline=False)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label=_("mod.jump"), url=message.jump_url))
        try:
            await ch.send(embed=e, view=view)
        except discord.HTTPException:
            pass

    # ── Reports (vom Kontextmenü „Nachricht melden“) ──
    async def send_report(self, interaction: discord.Interaction, message: discord.Message) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        channel = await mod_channel(interaction.guild, "report_channel")
        if channel is None:
            raise UserError("mod.report_no_channel")
        e = th.warning(_("mod.report_title"), message.content[:3000] or "*—*", user=message.author)
        e.add_field(name=_("mod.report_by"), value=interaction.user.mention, inline=True)
        e.add_field(name=_("mod.report_channel"), value=message.channel.mention, inline=True)
        e.add_field(name=_("mt.rp_author"), value=f"{message.author.mention}\n`{message.author.id}`", inline=True)
        view = report_view(_, message.channel.id, message.id, message.author.id)
        view.add_item(discord.ui.Button(label=_("mod.jump"), url=message.jump_url))
        cfg = await config.get(interaction.guild_id, "moderation")
        ping = f"<@&{cfg.id('duty_role')}>" if cfg.id("duty_role") and cfg.get("report_ping", True) else None
        await channel.send(content=ping, embed=e, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
        await log_event(interaction.guild_id, "moderation", "report", user=interaction.user, target_id=message.author.id,
                        channel_id=message.channel.id, content=message.content[:1000])
        await reply(interaction, th.success(_("common.success"), _("mod.report_sent")))

    async def report_action(self, interaction: discord.Interaction, act: str, ch_id: int, msg_id: int, user_id: int) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        e = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        if act == "claim":
            if any(f.name == _("mt.rp_claimed_by") for f in e.fields):
                raise UserError("mt.rp_already")
            e.add_field(name=_("mt.rp_claimed_by"), value=interaction.user.mention, inline=False)
            await interaction.response.edit_message(embed=e)
            return
        if act == "del":
            ch = interaction.guild.get_channel(ch_id)
            try:
                await ch.get_partial_message(msg_id).delete()  # type: ignore[union-attr]
            except (discord.HTTPException, AttributeError):
                raise UserError("mt.rp_gone") from None
            e.add_field(name=_("mt.rp_deleted"), value=interaction.user.mention, inline=True)
            await interaction.response.edit_message(embed=e)
            return
        if act == "warn":
            from app.bot.modules.moderation import WarnModal
            member = interaction.guild.get_member(user_id)
            if member is None:
                raise UserError("mod.err_not_member")
            await interaction.response.send_modal(WarnModal(member, _("mod.warn_modal_title", user=member.display_name)[:45], _("mod.f_reason")))
            return
        e.colour = discord.Colour.green()
        e.add_field(name=_("mt.rp_done_by"), value=f"{interaction.user.mention} · {ts(utcnow(), 'R')}", inline=False)
        view = report_view(_, ch_id, msg_id, user_id, done=True)
        await interaction.response.edit_message(embed=e, view=view)

    # ── Notizen ──
    async def add_note(self, interaction: discord.Interaction, target: discord.abc.User, text: str) -> None:
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        async with session_scope() as db:
            db.add(UserNote(guild_id=interaction.guild_id, user_id=target.id, author_id=interaction.user.id,
                            author_name=str(interaction.user)[:100], content=text[:2000]))
        await log_event(interaction.guild_id, "moderation", "note", user=interaction.user, target_id=target.id, content=text[:500])
        await reply(interaction, (await theme(interaction.guild)).success(_("mt.note_saved_title"), _("mt.note_saved", user=target.mention)))

    # ── Mod-Akte ──
    async def dossier(self, interaction: discord.Interaction, user: discord.Member | discord.User) -> None:
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        gid = interaction.guild_id
        async with SessionLocal() as db:
            cases = (await db.execute(select(ModCase).where(ModCase.guild_id == gid, ModCase.user_id == user.id)
                                      .order_by(ModCase.created_at.desc()))).scalars().all()
            notes = (await db.execute(select(UserNote).where(UserNote.guild_id == gid, UserNote.user_id == user.id)
                                      .order_by(UserNote.created_at.desc()).limit(3))).scalars().all()
            note_count = (await db.execute(select(func.count()).select_from(UserNote).where(UserNote.guild_id == gid, UserNote.user_id == user.id))).scalar()
        counts = Counter(c.action for c in cases)
        active_warns = sum(1 for c in cases if c.action == "warn" and c.active)
        e = th.embed(_("mt.dossier_title", user=user.display_name), icon=False)
        e.set_thumbnail(url=user.display_avatar.url)
        e.add_field(name=_("mt.f_account"), value=f"{ts(user.created_at, 'D')} ({ts(user.created_at, 'R')})", inline=True)
        if isinstance(user, discord.Member) and user.joined_at:
            e.add_field(name=_("mt.f_joined"), value=f"{ts(user.joined_at, 'D')} ({ts(user.joined_at, 'R')})", inline=True)
        else:
            e.add_field(name=_("mt.f_joined"), value=_("mt.not_member"), inline=True)
        e.add_field(name=_("mt.f_id"), value=f"`{user.id}`", inline=True)
        summary = " · ".join(f"{mod.ACTION_ICONS.get(a, '•')} {n}" for a, n in counts.most_common()) or _("mt.clean")
        e.add_field(name=_("mt.f_cases", count=len(cases)), value=f"{summary}\n{_('mt.active_warns', count=active_warns)}", inline=False)
        if cases:
            e.add_field(name=_("mt.f_last_cases"), value="\n".join(
                f"`#{c.case_number}` {mod.ACTION_ICONS.get(c.action, '•')} {(c.reason or '—')[:60]} · {ts(c.created_at, 'R')}" for c in cases[:4]), inline=False)
        if notes:
            e.add_field(name=_("mt.f_notes", count=note_count), value="\n".join(
                f"📝 {n.content[:80]} · *{n.author_name}* {ts(n.created_at, 'R')}" for n in notes), inline=False)
        flags = account_flags(_, user)
        if str(user.id) in (await config.state(f"mt:watch:{gid}") or {}):
            flags.append(_("mt.flag_watch"))
        if await config.state(f"mt:q:{gid}:{user.id}") is not None:
            flags.append(_("mt.flag_quarantine"))
        if flags:
            e.add_field(name=_("mt.f_flags"), value="\n".join(flags), inline=False)
        if isinstance(user, discord.Member):
            roles = [r.mention for r in reversed(user.roles[1:])][:15]
            e.add_field(name=_("mt.f_roles", count=len(user.roles) - 1), value=" ".join(roles) or "—", inline=False)
        await reply(interaction, e, view=DossierView(user, _))

    @m.command(name="akte", description="Mod-Akte: Cases, Verwarnungen, Notizen, Account-Alter und Warnhinweise")
    async def cmd_dossier(self, interaction: discord.Interaction, user: discord.User):
        member = interaction.guild.get_member(user.id) or user
        await self.dossier(interaction, member)

    @m.command(name="notiz", description="Interne Notiz zu einem User speichern (nur Team sieht sie)")
    async def cmd_note(self, interaction: discord.Interaction, user: discord.User, text: app_commands.Range[str, 1, 1000]):
        await self.add_note(interaction, user, text)

    @m.command(name="notizen", description="Alle Notizen zu einem User")
    async def cmd_notes(self, interaction: discord.Interaction, user: discord.User):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        async with SessionLocal() as db:
            notes = (await db.execute(select(UserNote).where(UserNote.guild_id == interaction.guild_id, UserNote.user_id == user.id)
                                      .order_by(UserNote.created_at.desc()).limit(20))).scalars().all()
        text = "\n\n".join(f"📝 {n.content[:300]}\n╰ *{n.author_name}* · {ts(n.created_at, 'R')}" for n in notes) or _("mt.no_notes")
        await reply(interaction, (await theme(interaction.guild)).embed(_("mt.notes_title", user=user.display_name), text[:4000], icon=False))

    # ── Quarantäne ──
    async def _quarantine_role(self, guild: discord.Guild) -> discord.Role:
        cfg = await config.get(guild.id, "moderation")
        role = guild.get_role(cfg.id("quarantine_role") or 0)
        if role:
            return role
        _ = await i18n.for_guild(guild.id)
        role = await guild.create_role(name=_("mt.q_role_name"), colour=discord.Colour.dark_grey(), reason="Quarantäne-Rolle")
        deny = discord.PermissionOverwrite(send_messages=False, add_reactions=False, speak=False, connect=False,
                                           send_messages_in_threads=False, create_public_threads=False)
        for ch in guild.categories + [c for c in guild.channels if c.category is None and not isinstance(c, discord.CategoryChannel)]:
            try:
                await ch.set_permissions(role, overwrite=deny, reason="Quarantäne-Rolle")
            except discord.HTTPException:
                pass
        data = dict(cfg)
        data["quarantine_role"] = str(role.id)
        await config.save(guild.id, "moderation", settings=data)
        return role

    @m.command(name="quarantaene", description="User isolieren: alle Rollen weg, Quarantäne-Rolle drauf (umkehrbar)")
    async def cmd_quarantine(self, interaction: discord.Interaction, user: discord.Member, grund: app_commands.Range[str, 0, 500] = ""):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        mod.check_hierarchy(interaction.guild, interaction.user, user)
        key = f"mt:q:{interaction.guild_id}:{user.id}"
        if await config.state(key) is not None:
            raise UserError("mt.q_already")
        await interaction.response.defer(ephemeral=True)
        role = await self._quarantine_role(interaction.guild)
        me = interaction.guild.me.top_role
        removable = [r for r in user.roles[1:] if r < me and not r.managed and r != role]
        await config.set_state(key, [r.id for r in removable])
        try:
            await user.edit(roles=[r for r in user.roles if r.managed or r >= me] + [role], reason=f"Quarantäne: {grund or '-'}")
        except discord.HTTPException:
            await config.set_state(key, None)
            raise UserError("mod.err_forbidden") from None
        case = await mod.create_case(interaction.guild, "quarantine", user, interaction.user, grund, active=True)
        await mod.send_modlog(interaction.guild, await mod.case_embed(interaction.guild, case, user))
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(
            _("mt.q_done_title"), _("mt.q_done", user=user.mention, count=len(removable))), ephemeral=True)

    @m.command(name="freilassen", description="User aus der Quarantäne holen und alte Rollen zurückgeben")
    async def cmd_release(self, interaction: discord.Interaction, user: discord.Member):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        key = f"mt:q:{interaction.guild_id}:{user.id}"
        saved = await config.state(key)
        if saved is None:
            raise UserError("mt.q_not")
        await interaction.response.defer(ephemeral=True)
        cfg = await config.get(interaction.guild_id, "moderation")
        qrole = interaction.guild.get_role(cfg.id("quarantine_role") or 0)
        roles = [r for r in (interaction.guild.get_role(int(x)) for x in saved) if r and r < interaction.guild.me.top_role]
        keep = [r for r in user.roles if r != qrole and (r.managed or r >= interaction.guild.me.top_role or r.is_default())]
        try:
            await user.edit(roles=list({r.id: r for r in keep + roles}.values()), reason=f"Quarantäne beendet von {interaction.user}")
        except discord.HTTPException:
            raise UserError("mod.err_forbidden") from None
        await config.set_state(key, None)
        case = await mod.create_case(interaction.guild, "unquarantine", user, interaction.user, "", active=False)
        await mod.send_modlog(interaction.guild, await mod.case_embed(interaction.guild, case, user))
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(
            _("common.success"), _("mt.q_released", user=user.mention, count=len(roles))), ephemeral=True)

    # ── Watchlist ──
    @m.command(name="watch", description="User beobachten: seine Nachrichten landen im Watch-Channel")
    async def cmd_watch(self, interaction: discord.Interaction, user: discord.User, grund: app_commands.Range[str, 0, 200] = ""):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        key = f"mt:watch:{interaction.guild_id}"
        data = dict(await config.state(key) or {})
        data[str(user.id)] = {"reason": grund, "by": interaction.user.id, "at": utcnow().isoformat()}
        await config.set_state(key, data)
        await log_event(interaction.guild_id, "moderation", "watch", user=interaction.user, target_id=user.id, content=grund)
        await reply(interaction, (await theme(interaction.guild)).success(_("mt.watch_on_title"), _("mt.watch_on", user=user.mention)))

    @m.command(name="unwatch", description="User nicht mehr beobachten")
    async def cmd_unwatch(self, interaction: discord.Interaction, user: discord.User):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        key = f"mt:watch:{interaction.guild_id}"
        data = dict(await config.state(key) or {})
        if data.pop(str(user.id), None) is None:
            raise UserError("mt.watch_not")
        await config.set_state(key, data)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("mt.watch_off", user=user.mention)))

    @m.command(name="watchlist", description="Alle beobachteten User")
    async def cmd_watchlist(self, interaction: discord.Interaction):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        data = await config.state(f"mt:watch:{interaction.guild_id}") or {}
        text = "\n".join(f"👁️ <@{uid}> · {v.get('reason') or '—'} · <@{v.get('by')}>" for uid, v in list(data.items())[:40]) or _("mt.watch_empty")
        await reply(interaction, (await theme(interaction.guild)).embed(_("mt.watch_title", count=len(data)), text, icon=False))

    # ── Snipe ──
    @m.command(name="snipe", description="Zuletzt gelöschte Nachrichten in diesem Channel (letzte Stunde)")
    async def cmd_snipe(self, interaction: discord.Interaction):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        items = [x for x in self._snipes.get(interaction.channel_id, []) if time.time() - x[0] < SNIPE_KEEP]
        if not items:
            raise UserError("mt.snipe_empty")
        lines = []
        for at, uid, name, content, files in reversed(items):
            extra = f"\n╰ 📎 {', '.join(files)[:200]}" if files else ""
            lines.append(f"**{name}** (<@{uid}>) · <t:{int(at)}:R>\n{content[:300] or '*—*'}{extra}")
        await reply(interaction, (await theme(interaction.guild)).embed(_("mt.snipe_title"), "\n\n".join(lines)[:4000], icon=False))

    # ── Dienst-Modus ──
    @m.command(name="dienst", description="Dienst an/aus – du bekommst die „Mod im Dienst“-Rolle und wirst bei Mod-Rufen gepingt")
    async def cmd_duty(self, interaction: discord.Interaction):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        cfg = await config.get(interaction.guild_id, "moderation")
        role = interaction.guild.get_role(cfg.id("duty_role") or 0)
        if role is None:
            raise UserError("mt.duty_no_role")
        member: discord.Member = interaction.user  # type: ignore[assignment]
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Dienst beendet")
                text = _("mt.duty_off")
            else:
                await member.add_roles(role, reason="Dienst gestartet")
                text = _("mt.duty_on")
        except discord.HTTPException:
            raise UserError("mod.err_forbidden") from None
        await log_event(interaction.guild_id, "moderation", "duty", user=member, content=text)
        await reply(interaction, (await theme(interaction.guild)).success(_("mt.duty_title"), text))

    @m.command(name="team", description="Wer vom Team ist gerade online und im Dienst?")
    async def cmd_team(self, interaction: discord.Interaction):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        cfg = await config.get(interaction.guild_id, "moderation")
        duty = interaction.guild.get_role(cfg.id("duty_role") or 0)
        staff = [m for m in interaction.guild.members if not m.bot and await is_staff(m)]
        icon = {discord.Status.online: "🟢", discord.Status.idle: "🌙", discord.Status.dnd: "⛔"}
        lines = []
        for m in sorted(staff, key=lambda x: (duty not in x.roles if duty else True, x.status == discord.Status.offline, x.display_name.lower()))[:40]:
            tag = f" · 🛡️ {_('mt.on_duty')}" if duty and duty in m.roles else ""
            lines.append(f"{icon.get(m.status, '⚫')} {m.mention}{tag}")
        await reply(interaction, (await theme(interaction.guild)).embed(_("mt.team_title", count=len(staff)), "\n".join(lines) or "—", icon=False))

    # ── Statistik ──
    @m.command(name="stats", description="Mod-Aktivität: wer hat wie viele Cases gemacht")
    @app_commands.describe(tage="Zeitraum in Tagen (Standard 30)")
    async def cmd_stats(self, interaction: discord.Interaction, tage: app_commands.Range[int, 1, 365] = 30):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        since = utcnow() - timedelta(days=tage)
        async with SessionLocal() as db:
            rows = (await db.execute(select(ModCase.moderator_id, ModCase.action, func.count()).where(
                ModCase.guild_id == interaction.guild_id, ModCase.created_at >= since, ModCase.source != "automod")
                .group_by(ModCase.moderator_id, ModCase.action))).all()
        per: dict[int, Counter] = {}
        for uid, action, n in rows:
            per.setdefault(uid, Counter())[action] += n
        if not per:
            raise UserError("mt.stats_empty")
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, c) in enumerate(sorted(per.items(), key=lambda x: -sum(x[1].values()))[:15]):
            detail = " ".join(f"{mod.ACTION_ICONS.get(a, '•')}{n}" for a, n in c.most_common(5))
            lines.append(f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{uid}> · **{sum(c.values())}** · {detail}")
        await reply(interaction, (await theme(interaction.guild)).embed(_("mt.stats_title", days=tage), "\n".join(lines), icon=False), ephemeral=False)

    # ── Lockdown ──
    @m.command(name="lockdown", description="Ganzen Server sperren/entsperren (@everyone kann nicht mehr schreiben)")
    @app_commands.describe(an="An oder aus", grund="Grund (wird angezeigt)")
    async def cmd_lockdown(self, interaction: discord.Interaction, an: bool, grund: app_commands.Range[str, 0, 300] = ""):
        await require_staff(interaction)
        if not interaction.user.guild_permissions.manage_channels:  # type: ignore[union-attr]
            raise UserError("errors.no_permission")
        _ = await i18n.for_guild(interaction.guild_id)
        guild = interaction.guild
        key = f"mt:lock:{guild.id}"
        saved = await config.state(key)
        if an and saved:
            raise UserError("mt.lock_already")
        if not an and not saved:
            raise UserError("mt.lock_not")
        if an and not await confirm(interaction, _("mod.confirm_title"), _("mt.lock_confirm")):
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        everyone = guild.default_role
        th = await theme(guild)
        changed = 0
        if an:
            state = {}
            for ch in guild.text_channels:
                ow = ch.overwrites_for(everyone)
                if ow.send_messages is False or not ch.permissions_for(everyone).view_channel:
                    continue
                state[str(ch.id)] = ow.send_messages
                ow.send_messages = False
                try:
                    await ch.set_permissions(everyone, overwrite=ow, reason=f"Lockdown: {grund or '-'}")
                    changed += 1
                except discord.HTTPException:
                    state.pop(str(ch.id), None)
            await config.set_state(key, state)
            notice = th.error(_("mt.lock_notice_title"), grund or _("mt.lock_notice"))
            try:
                await interaction.channel.send(embed=notice)
            except discord.HTTPException:
                pass
        else:
            for cid, old in (saved or {}).items():
                ch = guild.get_channel(int(cid))
                if not isinstance(ch, discord.TextChannel):
                    continue
                ow = ch.overwrites_for(everyone)
                ow.send_messages = old
                try:
                    await ch.set_permissions(everyone, overwrite=None if ow.is_empty() else ow, reason="Lockdown beendet")
                    changed += 1
                except discord.HTTPException:
                    pass
            await config.set_state(key, None)
        await log_event(guild.id, "moderation", "lockdown_on" if an else "lockdown_off", user=interaction.user, content=grund)
        await mod.send_modlog(guild, th.warning(_("mt.lock_log_on" if an else "mt.lock_log_off"), _("mt.lock_log", user=interaction.user.mention, count=changed)))
        await interaction.followup.send(embed=th.success(_("common.success"), _("mt.lock_done_on" if an else "mt.lock_done_off", count=changed)), ephemeral=True)

    # ── Massban ──
    @m.command(name="massban", description="Mehrere User per ID auf einmal bannen (z. B. bei Raids)")
    @app_commands.describe(ids="User-IDs, getrennt mit Leerzeichen oder Komma", grund="Grund")
    async def cmd_massban(self, interaction: discord.Interaction, ids: str, grund: app_commands.Range[str, 0, 300] = "Raid"):
        if not interaction.user.guild_permissions.ban_members:  # type: ignore[union-attr]
            raise UserError("errors.no_permission")
        _ = await i18n.for_guild(interaction.guild_id)
        found = list(dict.fromkeys(int(x) for x in re.findall(r"\d{15,21}", ids)))[:200]
        if not found:
            raise UserError("mod.err_invalid_id")
        protected = {interaction.user.id, interaction.guild.owner_id, self.bot.user.id}
        targets = [discord.Object(i) for i in found if i not in protected]
        if not await confirm(interaction, _("mod.confirm_title"), _("mt.massban_confirm", count=len(targets))):
            return
        try:
            result = await interaction.guild.bulk_ban(targets, reason=f"{interaction.user} | {grund}"[:512], delete_message_seconds=86400)
            banned = len(result.banned)
        except discord.HTTPException:
            raise UserError("mod.err_forbidden") from None
        for obj in result.banned:
            await mod.create_case(interaction.guild, "ban", obj.id, interaction.user, f"Massban: {grund}", source="command")
        await log_event(interaction.guild_id, "moderation", "massban", user=interaction.user, content=f"{banned} User: {grund}")
        th = await theme(interaction.guild)
        await mod.send_modlog(interaction.guild, th.error(_("mt.massban_log"), _("mt.massban_done", count=banned, user=interaction.user.mention)))
        await interaction.followup.send(embed=th.success(_("common.success"), _("mt.massban_done", count=banned, user=interaction.user.mention)), ephemeral=True)

    # ── Voice ──
    @m.command(name="voice-move", description="Alle aus einem Voice-Channel in einen anderen verschieben")
    async def cmd_voice_move(self, interaction: discord.Interaction, von: discord.VoiceChannel, nach: discord.VoiceChannel):
        if not interaction.user.guild_permissions.move_members:  # type: ignore[union-attr]
            raise UserError("errors.no_permission")
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        moved = 0
        for m in list(von.members):
            try:
                await m.move_to(nach, reason=f"Voice-Move von {interaction.user}")
                moved += 1
            except discord.HTTPException:
                pass
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(_("common.success"), _("mt.voice_moved", count=moved, channel=nach.mention)), ephemeral=True)

    @m.command(name="voice-leeren", description="Alle aus einem Voice-Channel rauswerfen")
    async def cmd_voice_clear(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        if not interaction.user.guild_permissions.move_members:  # type: ignore[union-attr]
            raise UserError("errors.no_permission")
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        kicked = 0
        for m in list(channel.members):
            try:
                await m.move_to(None, reason=f"Voice geleert von {interaction.user}")
                kicked += 1
            except discord.HTTPException:
                pass
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(_("common.success"), _("mt.voice_cleared", count=kicked, channel=channel.mention)), ephemeral=True)

    # ── Neue Mitglieder ──
    @m.command(name="neu", description="Die neuesten Mitglieder mit Warnhinweisen (neuer Account, kein Avatar)")
    async def cmd_new(self, interaction: discord.Interaction):
        await require_staff(interaction)
        _ = await i18n.for_guild(interaction.guild_id)
        members = sorted([m for m in interaction.guild.members if m.joined_at and not m.bot], key=lambda m: m.joined_at, reverse=True)[:20]
        lines = []
        for m in members:
            flags = account_flags(_, m)
            lines.append(f"{'⚠️' if flags else '✅'} {m.mention} · {_('mt.joined')} {ts(m.joined_at, 'R')} · {_('mt.account')} {ts(m.created_at, 'R')}"
                         + (f"\n╰ {' · '.join(flags)}" if flags else ""))
        await reply(interaction, (await theme(interaction.guild)).embed(_("mt.new_title"), "\n".join(lines)[:4000] or "—", icon=False))

    # ── Mod-Ruf (für alle) ──
    @app_commands.command(name="modruf", description="Ruft das Mod-Team – nur bei echten Problemen!")
    @app_commands.guild_only()
    @app_commands.describe(grund="Was ist los?", user="Um wen geht es? (optional)")
    async def modcall(self, interaction: discord.Interaction, grund: app_commands.Range[str, 5, 500], user: discord.Member | None = None):
        _ = await i18n.for_guild(interaction.guild_id)
        cfg = await config.get(interaction.guild_id, "moderation")
        cooldown = int(cfg.get("modcall_cooldown") or 10) * 60
        key = (interaction.guild_id, interaction.user.id)
        left = self._calls.get(key, 0) + cooldown - time.monotonic()
        if left > 0:
            raise UserError("mt.call_cooldown", minutes=max(1, round(left / 60)))
        ch = await mod_channel(interaction.guild, "modcall_channel")
        if ch is None:
            raise UserError("mod.report_no_channel")
        self._calls[key] = time.monotonic()
        th = await theme(interaction.guild)
        e = th.warning(_("mt.call_title"), grund, user=interaction.user)
        e.add_field(name=_("mt.call_from"), value=interaction.user.mention, inline=True)
        e.add_field(name=_("mod.report_channel"), value=interaction.channel.mention, inline=True)
        if user:
            e.add_field(name=_("mt.call_about"), value=f"{user.mention}\n`{user.id}`", inline=True)
        view = discord.ui.View(timeout=None)
        view.add_item(ClaimCall(interaction.user.id, _("mt.call_claim")))
        view.add_item(discord.ui.Button(label=_("mt.call_goto"), url=interaction.channel.jump_url))
        role_id = cfg.id("duty_role")
        await ch.send(content=f"<@&{role_id}>" if role_id else None, embed=e, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
        await log_event(interaction.guild_id, "moderation", "modcall", user=interaction.user, channel_id=interaction.channel_id, content=grund)
        await reply(interaction, th.success(_("mt.call_sent_title"), _("mt.call_sent")))


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(ReportAction, ClaimCall)
    cog = ModTools(bot)
    await bot.add_cog(cog)

    @app_commands.context_menu(name="Mod-Akte")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def ctx_dossier(interaction: discord.Interaction, user: discord.Member):
        try:
            await cog.dossier(interaction, user)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "dossier")

    @app_commands.context_menu(name="Löschen & Verwarnen")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def ctx_delete_warn(interaction: discord.Interaction, message: discord.Message):
        try:
            await require_staff(interaction)
            _ = await i18n.for_guild(interaction.guild_id)
            if not isinstance(message.author, discord.Member):
                raise UserError("mod.err_not_member")
            content = message.content[:200]
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            reason = _("mt.delwarn_reason", content=content or "—")
            case = await mod.execute(interaction.guild, "warn", message.author, interaction.user, reason)
            await reply(interaction, await mod.case_embed(interaction.guild, case, message.author))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "delwarn")

    for cm in (ctx_dossier, ctx_delete_warn):
        cm.extras["module"] = "moderation"
        cm.extras["help_category"] = "moderation"
        bot.tree.add_command(cm)
