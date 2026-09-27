"""Ticket-System: Panel, Kategorien, Modal, Claim, Close/Reopen, Lock, Priorität, Transfer, Notes, Transcripts."""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
from datetime import timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from app.bot.ui import confirm, handle_exception, reply
from app.core.embeds import hex_to_color, theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import fill, i18n
from app.core.metrics import metrics
from app.core.records import log_event
from app.core.timeutil import ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Ticket, TicketCategory, TicketNote
from app.services.guilds import next_number
from app.services.notify import is_staff, send_log

log = logging.getLogger("nova.tickets")
PRIORITIES = {"low": "🟢", "normal": "🔵", "high": "🟠", "urgent": "🔴"}


# ───────────────────────── Transcript ─────────────────────────
def _md(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"\*(.+?)\*", r"<i>\1</i>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"(https?://[^\s<]+)", r'<a href="\1" rel="noopener" target="_blank">\1</a>', t)
    return t.replace("\n", "<br>")


async def build_transcript(channel: discord.TextChannel, ticket: Ticket, guild: discord.Guild) -> tuple[str, int]:
    rows = []
    count = 0
    async for m in channel.history(limit=5000, oldest_first=True):
        count += 1
        parts = [f'<div class="c">{_md(m.content)}</div>' if m.content else ""]
        for e in m.embeds:
            parts.append(f'<div class="e"><b>{html.escape(e.title or "")}</b><br>{_md(e.description or "")}</div>')
            for f in e.fields:
                parts.append(f'<div class="e f"><b>{html.escape(f.name)}</b><br>{_md(f.value)}</div>')
        for a in m.attachments:
            if a.content_type and a.content_type.startswith("image/"):
                parts.append(f'<a href="{html.escape(a.url)}" target="_blank"><img src="{html.escape(a.url)}" alt=""></a>')
            else:
                parts.append(f'<a class="att" href="{html.escape(a.url)}" target="_blank">📎 {html.escape(a.filename)}</a>')
        bot_tag = '<span class="tag">BOT</span>' if m.author.bot else ""
        rows.append(
            f'<div class="m"><img class="a" src="{html.escape(m.author.display_avatar.with_size(64).url)}" alt="">'
            f'<div><div class="h"><span class="n">{html.escape(m.author.display_name)}</span>{bot_tag}'
            f'<span class="t">{m.created_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")} UTC</span></div>{"".join(parts)}</div></div>'
        )
    doc = f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ticket #{ticket.number} – {html.escape(guild.name)}</title><style>
body{{margin:0;background:#0b0b10;color:#e7e7ee;font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
header{{padding:24px 28px;background:linear-gradient(135deg,#1b1530,#0f0f18);border-bottom:1px solid #26263a}}
header h1{{margin:0 0 6px;font-size:20px}} header p{{margin:0;color:#9a9ab0;font-size:13px}}
main{{padding:12px 20px;max-width:980px}} .m{{display:flex;gap:12px;padding:10px 8px;border-radius:10px}} .m:hover{{background:#13131c}}
.a{{width:40px;height:40px;border-radius:50%;flex:none}} .h{{display:flex;gap:8px;align-items:baseline}} .n{{font-weight:600}}
.t{{color:#6f6f86;font-size:12px}} .tag{{background:#5865f2;color:#fff;font-size:10px;padding:1px 5px;border-radius:4px}}
.e{{border-left:4px solid #8b5cf6;background:#15151f;padding:8px 12px;border-radius:6px;margin-top:6px}}
img:not(.a){{max-width:420px;border-radius:8px;margin-top:6px}} code{{background:#1d1d29;padding:1px 5px;border-radius:4px}}
a{{color:#a78bfa}} .att{{display:inline-block;margin-top:6px}}
</style></head><body><header><h1>🎫 Ticket #{ticket.number} · {html.escape(ticket.category_label or "")}</h1>
<p>{html.escape(guild.name)} · {html.escape(ticket.opener_name)} · {count} Nachrichten · {ticket.created_at.strftime("%d.%m.%Y %H:%M")} UTC</p>
<p>{_md(ticket.subject or "")}</p></header><main>{"".join(rows)}</main></body></html>"""
    return doc, count


# ───────────────────────── Persistente Buttons ─────────────────────────
class TicketOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:tk:open:(?P<cat>\d+)"):
    def __init__(self, cat_id: int, label: str | None = None, emoji: str | None = None, style=discord.ButtonStyle.primary):
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"nova:tk:open:{cat_id}"))
        self.cat_id = cat_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["cat"]))

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.client.get_cog("Tickets").open_modal(interaction, self.cat_id)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "ticket_open")


class TicketSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"nova:tk:select"):
    def __init__(self, options: list[discord.SelectOption] | None = None, placeholder: str | None = None):
        super().__init__(discord.ui.Select(custom_id="nova:tk:select", options=options or [discord.SelectOption(label="…", value="0")],
                                           placeholder=placeholder))

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Select, match, /):
        inst = cls()
        inst.item = item
        return inst

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.client.get_cog("Tickets").open_modal(interaction, int(self.item.values[0]))  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "ticket_select")


# ───────────────────────── Channel-Namen ─────────────────────────
STATE_PREFIX = {"open": "", "claimed": "✅-", "closed": "🔒-"}


def slug(text: str) -> str:
    text = (text or "").lower().split("#")[0]
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def channel_name(fmt: str | None, category: str, username: str, number: int, state: str = "open") -> str:
    """z. B. entbannungsantrag-maxmuster-0012 · übernommen: ✅-entbannungsantrag-maxmuster-0012"""
    base = fill(fmt or "{category}-{username}-{number}", number=f"{number:04d}", username=slug(username)[:20] or "user",
                category=slug(category)[:30] or "ticket")
    base = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9\-_]", "-", base.lower())).strip("-") or f"ticket-{number:04d}"
    return (STATE_PREFIX.get(state, "") + base)[:95]


def control_view(_, claimed_name: str | None = None) -> discord.ui.View:
    """Buttons im Ticket: Übernehmen (nur einmal – danach grau mit Name), Schließen, Verwalten."""
    v = discord.ui.View(timeout=None)
    claim = TicketAction("claim", (_("tickets.claimed_by_short", name=claimed_name)[:80]) if claimed_name else _("tickets.btn_claim"))
    if claimed_name:
        claim.item.disabled, claim.item.style, claim.item.emoji = True, discord.ButtonStyle.secondary, "✅"
    v.add_item(claim)
    v.add_item(TicketAction("close", _("tickets.btn_close")))
    v.add_item(TicketAction("manage", _("tickets.btn_manage")))
    return v


class TicketAction(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:tk:(?P<action>claim|close|lock|reopen|delete|transcript|manage)"):
    STYLES = {"claim": (discord.ButtonStyle.success, "🙋"), "close": (discord.ButtonStyle.danger, "🔒"),
              "lock": (discord.ButtonStyle.secondary, "🔐"), "reopen": (discord.ButtonStyle.success, "🔓"),
              "delete": (discord.ButtonStyle.danger, "🗑️"), "transcript": (discord.ButtonStyle.secondary, "📄"),
              "manage": (discord.ButtonStyle.secondary, "⚙️")}

    def __init__(self, action: str, label: str | None = None):
        style, emoji = self.STYLES[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"nova:tk:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog: Tickets = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
        try:
            ticket = await cog.ticket_for(interaction.channel_id)
            if self.action == "close":
                await interaction.response.send_modal(CloseModal(cog, ticket.id, await i18n.for_guild(interaction.guild_id)))
                return
            await cog.require_staff(interaction, ticket, allow_opener=self.action == "transcript")
            if self.action == "claim":
                await cog.claim(interaction.guild, ticket.id, interaction.user)
                _ = await i18n.for_guild(interaction.guild_id)
                await interaction.response.edit_message(view=control_view(_, interaction.user.display_name))
            elif self.action == "manage":
                _ = await i18n.for_guild(interaction.guild_id)
                th = await theme(interaction.guild)
                await interaction.response.send_message(embed=th.embed(_("tickets.manage_title"), _("tickets.manage_desc"), icon=False),
                                                        view=ManageView(cog, ticket.id, interaction.user.id, _), ephemeral=True)
            elif self.action == "lock":
                await cog.set_locked(interaction.guild, ticket.id, not ticket.locked, interaction.user)
                await interaction.response.defer()
            elif self.action == "reopen":
                await interaction.response.defer()
                await cog.reopen(interaction.guild, ticket.id, interaction.user)
            elif self.action == "delete":
                await interaction.response.defer()
                await cog.delete_channel(interaction.guild, ticket.id, interaction.user)
            elif self.action == "transcript":
                await interaction.response.defer(ephemeral=True)
                doc, _c = await build_transcript(interaction.channel, ticket, interaction.guild)
                await interaction.followup.send(file=discord.File(io.BytesIO(doc.encode()), f"ticket-{ticket.number}.html"), ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, f"ticket_{self.action}")


class PrioritySelect(discord.ui.DynamicItem[discord.ui.Select], template=r"nova:tk:prio"):
    def __init__(self, placeholder: str | None = None, labels: dict[str, str] | None = None):
        labels = labels or {k: k for k in PRIORITIES}
        opts = [discord.SelectOption(label=labels[k], value=k, emoji=e) for k, e in PRIORITIES.items()]
        super().__init__(discord.ui.Select(custom_id="nova:tk:prio", options=opts, placeholder=placeholder))

    @classmethod
    async def from_custom_id(cls, interaction, item: discord.ui.Select, match, /):
        inst = cls()
        inst.item = item
        return inst

    async def callback(self, interaction: discord.Interaction):
        cog: Tickets = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
        try:
            ticket = await cog.ticket_for(interaction.channel_id)
            await cog.require_staff(interaction, ticket)
            await cog.set_priority(interaction.guild, ticket.id, self.item.values[0], interaction.user)
            await interaction.response.defer()
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "ticket_priority")


class ManageView(discord.ui.View):
    """Nur für das Team sichtbar (ephemeral): Freigeben, Sperren, Transcript, Übergeben, User hinzufügen/entfernen, Priorität."""

    def __init__(self, cog: "Tickets", ticket_id: int, owner_id: int, _):
        super().__init__(timeout=600)
        self.cog, self.ticket_id, self.owner_id, self._ = cog, ticket_id, owner_id, _
        self.unclaim.label, self.lock.label, self.transcript.label = _("tickets.btn_unclaim"), _("tickets.btn_lock"), _("tickets.btn_transcript")
        self.transfer.placeholder, self.add.placeholder, self.remove.placeholder = _("tickets.ph_transfer"), _("tickets.ph_add"), _("tickets.ph_remove")
        self.prio.placeholder = _("tickets.prio_placeholder")
        self.prio.options = [discord.SelectOption(label=_("tickets.prio." + k), value=k, emoji=e) for k, e in PRIORITIES.items()]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await handle_exception(interaction, error, "ticket_manage")

    async def _done(self, interaction: discord.Interaction, key: str, **kw) -> None:
        th = await theme(interaction.guild)
        await interaction.response.send_message(embed=th.success(self._("common.success"), self._(key, **kw)), ephemeral=True)

    @discord.ui.button(emoji="↩️", style=discord.ButtonStyle.secondary, row=0)
    async def unclaim(self, interaction: discord.Interaction, _b):
        await self.cog.unclaim(interaction.guild, self.ticket_id, interaction.user)
        await self._done(interaction, "tickets.unclaimed_done")

    @discord.ui.button(emoji="🔐", style=discord.ButtonStyle.secondary, row=0)
    async def lock(self, interaction: discord.Interaction, _b):
        t = await self.cog.get(self.ticket_id)
        await self.cog.set_locked(interaction.guild, self.ticket_id, not t.locked, interaction.user)
        await self._done(interaction, "tickets.unlocked_done" if t.locked else "tickets.locked_done")

    @discord.ui.button(emoji="📄", style=discord.ButtonStyle.secondary, row=0)
    async def transcript(self, interaction: discord.Interaction, _b):
        await interaction.response.defer(ephemeral=True)
        t = await self.cog.get(self.ticket_id)
        doc, _c = await build_transcript(interaction.channel, t, interaction.guild)
        await interaction.followup.send(file=discord.File(io.BytesIO(doc.encode()), f"ticket-{t.number:04d}.html"), ephemeral=True)

    @discord.ui.select(cls=discord.ui.UserSelect, row=1)
    async def transfer(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        if not isinstance(member, discord.Member) or member.bot:
            raise UserError("tickets.staff_only")
        await self.cog.transfer(interaction.guild, self.ticket_id, member, interaction.user)
        await self._done(interaction, "tickets.transferred_done", user=member.mention)

    @discord.ui.select(cls=discord.ui.UserSelect, row=2)
    async def add(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        await self.cog.add_user(interaction.guild, self.ticket_id, member, interaction.user)  # type: ignore[arg-type]
        await self._done(interaction, "tickets.added_done", user=member.mention)

    @discord.ui.select(cls=discord.ui.UserSelect, row=3)
    async def remove(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        member = select.values[0]
        await self.cog.remove_user(interaction.guild, self.ticket_id, member, interaction.user)  # type: ignore[arg-type]
        await self._done(interaction, "tickets.removed_done", user=member.mention)

    @discord.ui.select(row=4, options=[discord.SelectOption(label="…", value="normal")])
    async def prio(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self.cog.set_priority(interaction.guild, self.ticket_id, select.values[0], interaction.user)
        await self._done(interaction, "tickets.prio_done", prio=f"{PRIORITIES[select.values[0]]} {self._('tickets.prio.' + select.values[0])}")


class OpenModal(discord.ui.Modal):
    """Formular mit den Fragen der gewählten Kategorie (max. 5) – Fallback: eine Frage."""

    def __init__(self, cog: "Tickets", category: TicketCategory, title: str, placeholder: str):
        super().__init__(title=title[:45])
        self.cog, self.category = cog, category
        questions = [x for x in (category.questions or []) if (x.get("label") or "").strip()][:5]
        if not questions:
            questions = [{"label": category.modal_question or "Was ist dein Anliegen?", "placeholder": placeholder, "style": "paragraph", "required": True}]
        self.inputs: list[discord.ui.TextInput] = []
        for x in questions:
            long = x.get("style") == "paragraph"
            ti = discord.ui.TextInput(label=x["label"][:45], style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                                      required=bool(x.get("required", True)), max_length=1000 if long else 200,
                                      placeholder=(x.get("placeholder") or None) and x["placeholder"][:100])
            self.inputs.append(ti)
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        answers = [(ti.label, ti.value.strip()) for ti in self.inputs if ti.value.strip()]
        subject = "\n\n".join(f"**{q}**\n{a}" for q, a in answers) if len(self.inputs) > 1 else (answers[0][1] if answers else "")
        channel = await self.cog.create_ticket(interaction.guild, interaction.user, self.category, subject, answers)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        await interaction.followup.send(embed=th.success(_("tickets.created_title"), _("tickets.created", channel=channel.mention)), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "ticket_create")


class CloseModal(discord.ui.Modal):
    def __init__(self, cog: "Tickets", ticket_id: int, _):
        super().__init__(title=_("tickets.close_title")[:45])
        self.cog, self.ticket_id = cog, ticket_id
        self.reason = discord.ui.TextInput(label=_("tickets.close_reason")[:45], required=False, max_length=500, style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ticket = await self.cog.get(self.ticket_id)
        if interaction.user.id != ticket.opener_id:
            await self.cog.require_staff(interaction, ticket)
        await self.cog.close(interaction.guild, self.ticket_id, interaction.user, self.reason.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "ticket_close")


from app.services.ticket_defaults import DEFAULT_CATEGORIES  # noqa: E402


async def ensure_default_categories(guild_id: int) -> int:
    """Legt die Standard-Kategorien an, wenn ein Server noch gar keine hat. Gibt die Anzahl neuer Kategorien zurück."""
    async with session_scope() as s:
        count = (await s.execute(select(func.count()).select_from(TicketCategory).where(TicketCategory.guild_id == guild_id))).scalar_one()
        if count:
            return 0
        for i, (label, emoji, desc, questions) in enumerate(DEFAULT_CATEGORIES):
            s.add(TicketCategory(guild_id=guild_id, label=label, emoji=emoji, description=desc, staff_role_ids=[], questions=questions,
                                 modal_question="Was ist dein Anliegen?", welcome_message="", position=i, enabled=True))
    return len(DEFAULT_CATEGORIES)


class TicketStartButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:tk:start"):
    """Ein Button im Panel → danach wählt der User die Kategorie (Support, Bewerbung, …)."""

    def __init__(self, label: str | None = None):
        super().__init__(discord.ui.Button(label=label, emoji="🎫", style=discord.ButtonStyle.primary, custom_id="nova:tk:start"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            if not await config.enabled(interaction.guild_id, "tickets"):
                raise UserError("errors.module_disabled")
            await ensure_default_categories(interaction.guild_id)
            async with SessionLocal() as s:
                cats = (await s.execute(select(TicketCategory).where(TicketCategory.guild_id == interaction.guild_id, TicketCategory.enabled.is_(True))
                                        .order_by(TicketCategory.position, TicketCategory.id))).scalars().all()
            if not cats:
                raise UserError("tickets.no_categories")
            _ = await i18n.for_guild(interaction.guild_id)
            th = await theme(interaction.guild)
            view = discord.ui.View(timeout=300)
            select_menu = discord.ui.Select(placeholder=_("tickets.select_placeholder"), options=[
                discord.SelectOption(label=c.label[:100], value=str(c.id), emoji=c.emoji or None, description=(c.description or None) and c.description[:100])
                for c in cats[:25]])

            async def chosen(i: discord.Interaction):
                try:
                    await i.client.get_cog("Tickets").open_modal(i, int(select_menu.values[0]))  # type: ignore[union-attr]
                except Exception as exc:  # noqa: BLE001
                    await handle_exception(i, exc, "ticket_choose")

            select_menu.callback = chosen
            view.add_item(select_menu)
            await interaction.response.send_message(embed=th.embed(_("tickets.choose_title"), _("tickets.choose_desc"), icon=False), view=view, ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "ticket_start")


# ───────────────────────── Cog ─────────────────────────
@app_commands.guild_only()
class Tickets(commands.Cog):
    module = "tickets"
    help_category = "tickets"

    ticket = app_commands.Group(name="ticket", description="Ticket-Verwaltung", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Lookup / Rechte ──
    async def get(self, ticket_id: int) -> Ticket:
        async with SessionLocal() as s:
            t = await s.get(Ticket, ticket_id)
        if t is None:
            raise UserError("tickets.not_found")
        return t

    async def ticket_for(self, channel_id: int | None) -> Ticket:
        async with SessionLocal() as s:
            t = (await s.execute(select(Ticket).where(Ticket.channel_id == channel_id).order_by(Ticket.id.desc()).limit(1))).scalar_one_or_none()
        if t is None:
            raise UserError("tickets.not_a_ticket")
        return t

    async def staff_roles(self, guild_id: int, category_id: int | None) -> list[int]:
        cfg = await config.get(guild_id, "tickets")
        roles = cfg.ids("staff_roles")
        if category_id:
            async with SessionLocal() as s:
                cat = await s.get(TicketCategory, category_id)
            if cat:
                roles += [int(r) for r in cat.staff_role_ids or []]
        return list(dict.fromkeys(roles))

    async def require_staff(self, interaction: discord.Interaction, ticket: Ticket, allow_opener: bool = False) -> None:
        if allow_opener and interaction.user.id == ticket.opener_id:
            return
        if not await is_staff(interaction.user, await self.staff_roles(interaction.guild_id, ticket.category_id)):  # type: ignore[arg-type]
            raise UserError("tickets.staff_only")

    # ── Panel ──
    async def send_panel(self, guild: discord.Guild, channel: discord.TextChannel | None = None) -> discord.Message:
        cfg = await config.get(guild.id, "tickets")
        channel = channel or guild.get_channel(cfg.id("panel_channel") or 0)
        if not isinstance(channel, discord.TextChannel):
            raise UserError("tickets.no_panel_channel")
        await ensure_default_categories(guild.id)
        async with SessionLocal() as s:
            cats = (await s.execute(select(TicketCategory).where(TicketCategory.guild_id == guild.id, TicketCategory.enabled.is_(True))
                                    .order_by(TicketCategory.position, TicketCategory.id))).scalars().all()
        if not cats:
            raise UserError("tickets.no_categories")
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = th.embed(cfg.get("panel_title"), cfg.get("panel_text"), icon=False, timestamp=False)
        e.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
        if cfg.get("panel_image"):
            e.set_image(url=cfg["panel_image"])
        view = discord.ui.View(timeout=None)
        view.add_item(TicketStartButton(cfg.get("panel_button") or _("tickets.open_button")))
        return await channel.send(embed=e, view=view)

    @ticket.command(name="panel", description="Postet das Ticket-Panel in diesen (oder einen gewählten) Channel (Admin)")
    async def panel_cmd(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        from app.services.notify import is_admin
        if not await is_admin(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("tickets.no_panel_channel")
        await interaction.response.defer(ephemeral=True)
        msg = await self.send_panel(interaction.guild, target)
        # Channel merken, damit Dashboard & /setup denselben nutzen
        cfg = dict(await config.get(interaction.guild_id, "tickets"))
        cfg["panel_channel"] = str(target.id)
        await config.save(interaction.guild_id, "tickets", settings=cfg, enabled=True)
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.followup.send(embed=(await theme(interaction.guild)).success(
            _("common.success"), _("tickets.panel_sent_in", channel=target.mention, url=msg.jump_url)), ephemeral=True)

    # ── Erstellen ──
    async def open_modal(self, interaction: discord.Interaction, cat_id: int) -> None:
        if not await config.enabled(interaction.guild_id, "tickets"):
            raise UserError("errors.module_disabled")
        async with SessionLocal() as s:
            cat = await s.get(TicketCategory, cat_id)
            open_count = (await s.execute(select(func.count()).select_from(Ticket).where(
                Ticket.guild_id == interaction.guild_id, Ticket.opener_id == interaction.user.id, Ticket.status == "open"))).scalar_one()
        if cat is None or cat.guild_id != interaction.guild_id or not cat.enabled:
            raise UserError("tickets.category_gone")
        cfg = await config.get(interaction.guild_id, "tickets")
        if open_count >= cfg.get("max_open", 2):
            raise UserError("tickets.max_open", max=cfg.get("max_open", 2))
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.send_modal(OpenModal(self, cat, f"{cat.emoji} {cat.label}", _("tickets.modal_placeholder")))

    async def create_ticket(self, guild: discord.Guild, user: discord.Member, cat: TicketCategory, subject: str,
                            answers: list[tuple[str, str]] | None = None) -> discord.TextChannel:
        cfg = await config.get(guild.id, "tickets")
        category = guild.get_channel(cat.category_channel_id or cfg.id("category") or 0)
        if category is not None and not isinstance(category, discord.CategoryChannel):
            category = None
        staff = [guild.get_role(r) for r in await self.staff_roles(guild.id, cat.id)]
        staff = [r for r in staff if r]
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True,
                                                  read_message_history=True, attach_files=True, embed_links=True),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        }
        for r in staff:
            overwrites[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, manage_messages=True)

        async with session_scope() as s:
            number = await next_number(s, guild.id, "next_ticket")
            name = channel_name(cfg.get("name_format"), cat.label, user.name, number)
            channel = await guild.create_text_channel(name, category=category, overwrites=overwrites,
                                                      topic=f"🎫 #{number} · {user} · {cat.label}", reason=f"Ticket #{number}")
            t = Ticket(guild_id=guild.id, number=number, channel_id=channel.id, category_id=cat.id, category_label=cat.label,
                       opener_id=user.id, opener_name=str(user), subject=subject, status="open", priority="normal", added_user_ids=[])
            s.add(t)
            await s.flush()
            ticket = t

        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        # Bewusst schlicht: Kategorie, Anliegen, ein Satz – mehr Optionen gibt's per /ticket und im Dashboard
        if answers and len(answers) > 1:
            e = th.embed(f"{cat.emoji} {cat.label}", cat.welcome_message or _("tickets.welcome_short"), icon=False, timestamp=False)
            for q, a in answers[:5]:
                e.add_field(name=q[:256], value=a[:1024], inline=len(a) <= 40)
        else:
            quote = "\n".join("> " + line for line in subject[:900].splitlines() if line.strip())
            e = th.embed(f"{cat.emoji} {cat.label}", f"{quote}\n\n{cat.welcome_message or _('tickets.welcome_short')}", icon=False, timestamp=False)
        e.set_footer(text=f"#{number:04d} · {user.display_name}", icon_url=user.display_avatar.url)
        view = control_view(_)
        ping = " ".join(r.mention for r in staff) if cfg.get("ping_staff", True) else ""
        msg = await channel.send(content=f"{user.mention} {ping}".strip(), embed=e, view=view,
                                 allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        metrics.incr(guild.id, "tickets_opened")
        bus.publish(guild.id, "ticket", {"action": "open", "id": ticket.id, "number": number})
        await log_event(guild.id, "ticket", "open", user=user, channel_id=channel.id, content=subject[:500], details={"number": number, "category": cat.label})
        await self._log(guild, "tickets.log_open", f"#{number:04d} · {user.mention} · {channel.mention}\n> {subject[:300]}", "success")
        return channel

    async def _log(self, guild: discord.Guild, key: str, desc: str, kind: str = "info") -> None:
        logcfg = await config.get(guild.id, "logging")
        if "ticket" not in (logcfg.get("events") or []):
            return
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        await send_log(guild, "default", th.embed(_(key), desc, kind=kind))

    async def _update(self, ticket_id: int, **fields) -> Ticket:
        async with session_scope() as s:
            t = await s.get(Ticket, ticket_id)
            if t is None:
                raise UserError("tickets.not_found")
            for k, v in fields.items():
                setattr(t, k, v)
        bus.publish(t.guild_id, "ticket", {"action": "update", "id": t.id, **{k: (str(v) if isinstance(v, int) and v > 2**40 else v) for k, v in fields.items() if k != "transcript_html"}})
        return t

    STATE_CATEGORIES = {"claimed": ("claimed_category", "〢 ÜBERNOMMENE TICKETS"), "closed": ("closed_category", "〢 GESCHLOSSENE TICKETS")}

    async def _state_category(self, guild: discord.Guild, cfg, state: str) -> discord.CategoryChannel | None:
        """Kategorie für übernommene/geschlossene Tickets – wird bei Bedarf automatisch (privat) angelegt."""
        key, title = self.STATE_CATEGORIES[state]
        existing = guild.get_channel(cfg.id(key) or 0)
        if isinstance(existing, discord.CategoryChannel):
            return existing
        existing = discord.utils.find(lambda c: c.name.lower() == title.lower(), guild.categories)
        if existing is None:
            ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                  guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
            for rid in cfg.ids("staff_roles"):
                if (r := guild.get_role(rid)):
                    ow[r] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
            try:
                existing = await guild.create_category(title, overwrites=ow, reason=f"Ablage für Tickets ({state})")
            except discord.HTTPException:
                return None
        data = dict(await config.get(guild.id, "tickets"))
        data[key] = str(existing.id)
        await config.save(guild.id, "tickets", settings=data)
        return existing

    async def _move(self, guild: discord.Guild, t: Ticket, state: str) -> None:
        """Ticket-Channel in die passende Kategorie verschieben: offen / übernommen / geschlossen."""
        channel = guild.get_channel(t.channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            return
        cfg = await config.get(guild.id, "tickets")
        if state in self.STATE_CATEGORIES:
            target = await self._state_category(guild, cfg, state)
        else:
            async with SessionLocal() as s:
                cat = await s.get(TicketCategory, t.category_id) if t.category_id else None
            target = guild.get_channel((cat.category_channel_id if cat else None) or cfg.id("category") or 0)
        if isinstance(target, discord.CategoryChannel) and channel.category_id != target.id and len(target.channels) < 50:
            try:
                await channel.edit(category=target, sync_permissions=False, reason=f"Ticket #{t.number}: {state}")
            except discord.HTTPException:
                pass
    def _rename_later(self, guild: discord.Guild, t: Ticket, state: str) -> None:
        """Channel mit Status-Emoji umbenennen – im Hintergrund, weil Discord nur 2 Umbenennungen / 10 Min erlaubt."""
        channel = guild.get_channel(t.channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            return

        async def run():
            cfg = await config.get(guild.id, "tickets")
            name = channel_name(cfg.get("name_format"), t.category_label, t.opener_name, t.number, state)
            if channel.name != name:
                try:
                    await asyncio.wait_for(channel.edit(name=name, reason=f"Ticket #{t.number}: {state}"), timeout=15)
                except (discord.HTTPException, asyncio.TimeoutError):
                    pass

        asyncio.create_task(run())

    async def _control_message(self, channel: discord.TextChannel) -> discord.Message | None:
        """Die (angepinnte) Ticket-Nachricht mit den Buttons finden."""
        try:
            for m in await channel.pins():
                if m.author.id == channel.guild.me.id and any(getattr(c, "custom_id", "") == "nova:tk:claim"
                                                              for row in m.components for c in getattr(row, "children", [])):
                    return m
        except discord.HTTPException:
            pass
        return None

    async def unclaim(self, guild: discord.Guild, ticket_id: int, actor: discord.abc.User) -> Ticket:
        t = await self.get(ticket_id)
        if not t.claimed_by:
            raise UserError("tickets.not_claimed")
        t = await self._update(ticket_id, claimed_by=None, claimed_name=None)
        _ = await i18n.for_guild(guild.id)
        channel = guild.get_channel(t.channel_id or 0)
        if isinstance(channel, discord.TextChannel) and (msg := await self._control_message(channel)):
            try:
                await msg.edit(view=control_view(_))
            except discord.HTTPException:
                pass
        await self._notice(guild, t, _("tickets.unclaimed", user=actor.mention))
        self._rename_later(guild, t, "open")
        await self._move(guild, t, "open")
        return t

    async def _notice(self, guild: discord.Guild, ticket: Ticket, text: str, kind: str = "info") -> None:
        channel = guild.get_channel(ticket.channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            th = await theme(guild)
            try:
                await channel.send(embed=th.embed(None, text, kind=kind))
            except discord.HTTPException:
                pass

    # ── Aktionen (auch vom Dashboard genutzt) ──
    async def claim(self, guild: discord.Guild, ticket_id: int, staff: discord.abc.User) -> Ticket:
        t = await self.get(ticket_id)
        if t.claimed_by:  # Nur einmal übernehmbar – Wechsel nur über „Übergeben“ oder „Freigeben“
            raise UserError("tickets.already_claimed", user=f"<@{t.claimed_by}>")
        t = await self._update(ticket_id, claimed_by=staff.id, claimed_name=str(staff), first_response_at=t.first_response_at or utcnow())
        _ = await i18n.for_guild(guild.id)
        await self._notice(guild, t, _("tickets.claimed", user=staff.mention), "success")
        await self._sync_buttons(guild, t, getattr(staff, "display_name", str(staff)))
        self._rename_later(guild, t, "claimed")
        await self._move(guild, t, "claimed")
        await log_event(guild.id, "ticket", "claim", user=staff, channel_id=t.channel_id, details={"number": t.number})
        return t

    async def _sync_buttons(self, guild: discord.Guild, t: Ticket, claimed_name: str | None) -> None:
        channel = guild.get_channel(t.channel_id or 0)
        if isinstance(channel, discord.TextChannel) and (msg := await self._control_message(channel)):
            _ = await i18n.for_guild(guild.id)
            try:
                await msg.edit(view=control_view(_, claimed_name))
            except discord.HTTPException:
                pass

    async def transfer(self, guild: discord.Guild, ticket_id: int, to: discord.Member, actor: discord.abc.User) -> Ticket:
        t = await self._update(ticket_id, claimed_by=to.id, claimed_name=str(to))
        channel = guild.get_channel(t.channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            await channel.set_permissions(to, view_channel=True, send_messages=True, read_message_history=True)
        await self._sync_buttons(guild, t, to.display_name)
        self._rename_later(guild, t, "claimed")
        await self._move(guild, t, "claimed")
        _ = await i18n.for_guild(guild.id)
        await self._notice(guild, t, _("tickets.transferred", user=to.mention, by=actor.mention))
        await log_event(guild.id, "ticket", "transfer", user=actor, target_id=to.id, details={"number": t.number})
        return t

    async def set_priority(self, guild: discord.Guild, ticket_id: int, priority: str, actor: discord.abc.User) -> Ticket:
        if priority not in PRIORITIES:
            raise UserError("errors.invalid_input", value=priority)
        t = await self._update(ticket_id, priority=priority)
        _ = await i18n.for_guild(guild.id)
        await self._notice(guild, t, _("tickets.priority_set", prio=f"{PRIORITIES[priority]} {_('tickets.prio.' + priority)}", user=actor.mention))
        return t

    async def set_locked(self, guild: discord.Guild, ticket_id: int, locked: bool, actor: discord.abc.User) -> Ticket:
        t = await self._update(ticket_id, locked=locked)
        channel = guild.get_channel(t.channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            for uid in [t.opener_id] + [int(u) for u in t.added_user_ids or []]:
                m = guild.get_member(uid)
                if m:
                    await channel.set_permissions(m, view_channel=True, read_message_history=True, send_messages=not locked)
        _ = await i18n.for_guild(guild.id)
        await self._notice(guild, t, _("tickets.locked" if locked else "tickets.unlocked", user=actor.mention), "warning" if locked else "success")
        return t

    async def add_user(self, guild: discord.Guild, ticket_id: int, member: discord.Member, actor: discord.abc.User) -> Ticket:
        t = await self.get(ticket_id)
        ids = list(dict.fromkeys([*(t.added_user_ids or []), str(member.id)]))
        t = await self._update(ticket_id, added_user_ids=ids)
        channel = guild.get_channel(t.channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            await channel.set_permissions(member, view_channel=True, send_messages=not t.locked, read_message_history=True, attach_files=True)
        _ = await i18n.for_guild(guild.id)
        await self._notice(guild, t, _("tickets.user_added", user=member.mention, by=actor.mention), "success")
        return t

    async def remove_user(self, guild: discord.Guild, ticket_id: int, member: discord.Member, actor: discord.abc.User) -> Ticket:
        t = await self.get(ticket_id)
        if member.id == t.opener_id:
            raise UserError("tickets.cant_remove_opener")
        t = await self._update(ticket_id, added_user_ids=[u for u in t.added_user_ids or [] if u != str(member.id)])
        channel = guild.get_channel(t.channel_id or 0)
        if isinstance(channel, discord.TextChannel):
            await channel.set_permissions(member, overwrite=None)
        _ = await i18n.for_guild(guild.id)
        await self._notice(guild, t, _("tickets.user_removed", user=member.mention, by=actor.mention), "warning")
        return t

    async def rename(self, guild: discord.Guild, ticket_id: int, name: str) -> Ticket:
        t = await self.get(ticket_id)
        channel = guild.get_channel(t.channel_id or 0)
        clean = re.sub(r"[^a-z0-9\-_]", "", name.lower().replace(" ", "-"))[:90]
        if not clean or not isinstance(channel, discord.TextChannel):
            raise UserError("errors.invalid_input", value=name)
        await channel.edit(name=clean)
        return t

    async def add_note(self, ticket_id: int, author: discord.abc.User, content: str) -> TicketNote:
        async with session_scope() as s:
            n = TicketNote(ticket_id=ticket_id, author_id=author.id, author_name=str(author), content=content[:2000])
            s.add(n)
            await s.flush()
            return n

    async def close(self, guild: discord.Guild, ticket_id: int, actor: discord.abc.User, reason: str = "") -> Ticket:
        t = await self.get(ticket_id)
        if t.status == "closed":
            raise UserError("tickets.already_closed")
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        cfg = await config.get(guild.id, "tickets")
        channel = guild.get_channel(t.channel_id or 0)
        doc, count = ("", 0)
        if isinstance(channel, discord.TextChannel):
            doc, count = await build_transcript(channel, t, guild)
        t = await self._update(ticket_id, status="closed", closed_at=utcnow(), closed_by=actor.id, closed_by_name=str(actor),
                               close_reason=reason or None, transcript_html=doc or None, transcript_count=count)
        metrics.incr(guild.id, "tickets_closed")
        await log_event(guild.id, "ticket", "close", user=actor, channel_id=t.channel_id, content=reason, details={"number": t.number})

        summary = th.embed(_("tickets.closed_title", number=f"{t.number:04d}"), kind="warning", icon=False)
        summary.add_field(name=_("tickets.opened_by"), value=f"<@{t.opener_id}>", inline=True)
        summary.add_field(name=_("tickets.closed_by"), value=actor.mention, inline=True)
        summary.add_field(name=_("tickets.category"), value=t.category_label or "—", inline=True)
        if t.claimed_by:
            summary.add_field(name=_("tickets.claimed_by"), value=f"<@{t.claimed_by}>", inline=True)
        summary.add_field(name=_("tickets.opened_at"), value=ts(t.created_at, "f"), inline=True)
        summary.add_field(name=_("tickets.close_reason"), value=reason or "—", inline=False)

        if doc:
            tchan = guild.get_channel(cfg.id("transcript_channel") or 0)
            if isinstance(tchan, discord.TextChannel):
                try:
                    await tchan.send(embed=summary, file=discord.File(io.BytesIO(doc.encode()), f"ticket-{t.number:04d}.html"))
                except discord.HTTPException:
                    pass
            opener = guild.get_member(t.opener_id)
            if cfg.get("dm_transcript", True) and opener:
                try:
                    await opener.send(embed=summary, file=discord.File(io.BytesIO(doc.encode()), f"ticket-{t.number:04d}.html"))
                except discord.HTTPException:
                    pass
        await self._log(guild, "tickets.log_close", f"#{t.number:04d} · {actor.mention} · {reason or '—'}", "warning")

        if isinstance(channel, discord.TextChannel):
            for uid in [t.opener_id] + [int(u) for u in t.added_user_ids or []]:
                m = guild.get_member(uid)
                if m:
                    try:
                        await channel.set_permissions(m, view_channel=True, read_message_history=True, send_messages=False)
                    except discord.HTTPException:
                        pass
            view = discord.ui.View(timeout=None)
            view.add_item(TicketAction("reopen", _("tickets.btn_reopen")))
            view.add_item(TicketAction("transcript", _("tickets.btn_transcript")))
            view.add_item(TicketAction("delete", _("tickets.btn_delete")))
            try:
                await channel.send(embed=summary, view=view)
                self._rename_later(guild, t, "closed")
                await self._move(guild, t, "closed")
            except discord.HTTPException:
                pass
        return t

    async def reopen(self, guild: discord.Guild, ticket_id: int, actor: discord.abc.User) -> Ticket:
        t = await self.get(ticket_id)
        channel = guild.get_channel(t.channel_id or 0)
        if not isinstance(channel, discord.TextChannel):
            raise UserError("tickets.channel_gone")
        t = await self._update(ticket_id, status="open", closed_at=None, locked=False)
        for uid in [t.opener_id] + [int(u) for u in t.added_user_ids or []]:
            m = guild.get_member(uid)
            if m:
                await channel.set_permissions(m, view_channel=True, read_message_history=True, send_messages=True, attach_files=True)
        _ = await i18n.for_guild(guild.id)
        await self._notice(guild, t, _("tickets.reopened", user=actor.mention), "success")
        self._rename_later(guild, t, "claimed" if t.claimed_by else "open")
        await self._move(guild, t, "claimed" if t.claimed_by else "open")
        await log_event(guild.id, "ticket", "reopen", user=actor, channel_id=t.channel_id, details={"number": t.number})
        return t

    async def delete_channel(self, guild: discord.Guild, ticket_id: int, actor: discord.abc.User) -> None:
        t = await self.get(ticket_id)
        if t.status != "closed":
            await self.close(guild, ticket_id, actor, "")
        channel = guild.get_channel(t.channel_id or 0)
        await self._update(ticket_id, channel_id=None)
        await log_event(guild.id, "ticket", "delete", user=actor, details={"number": t.number})
        if channel:
            await channel.delete(reason=f"Ticket #{t.number} gelöscht von {actor}")

    # ── Staff-Antwortzeit messen ──
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        topic = getattr(message.channel, "topic", None) or ""
        if not topic.startswith("🎫"):
            return
        async with session_scope() as s:
            t = (await s.execute(select(Ticket).where(Ticket.channel_id == message.channel.id, Ticket.status == "open"))).scalar_one_or_none()
            if t and t.first_response_at is None and message.author.id != t.opener_id:
                t.first_response_at = utcnow()

    # ── Slash-Commands ──
    async def _ctx(self, interaction: discord.Interaction) -> Ticket:
        t = await self.ticket_for(interaction.channel_id)
        await self.require_staff(interaction, t)
        return t

    async def _ack(self, interaction: discord.Interaction) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("common.success"), _("common.done")))

    @ticket.command(name="close", description="Schließt dieses Ticket")
    async def close_cmd(self, interaction: discord.Interaction, reason: str = ""):
        t = await self.ticket_for(interaction.channel_id)
        if interaction.user.id != t.opener_id:
            await self.require_staff(interaction, t)
        _ = await i18n.for_guild(interaction.guild_id)
        if not await confirm(interaction, _("tickets.close_title"), _("tickets.close_confirm")):
            return
        await self.close(interaction.guild, t.id, interaction.user, reason)

    @ticket.command(name="reopen", description="Öffnet dieses Ticket erneut")
    async def reopen_cmd(self, interaction: discord.Interaction):
        t = await self._ctx(interaction)
        await self._ack(interaction)
        await self.reopen(interaction.guild, t.id, interaction.user)

    @ticket.command(name="claim", description="Übernimmt dieses Ticket")
    async def claim_cmd(self, interaction: discord.Interaction):
        t = await self._ctx(interaction)
        await self.claim(interaction.guild, t.id, interaction.user)
        await self._ack(interaction)

    @ticket.command(name="add", description="Fügt einen User zum Ticket hinzu")
    async def add_cmd(self, interaction: discord.Interaction, user: discord.Member):
        t = await self._ctx(interaction)
        await self.add_user(interaction.guild, t.id, user, interaction.user)
        await self._ack(interaction)

    @ticket.command(name="remove", description="Entfernt einen User aus dem Ticket")
    async def remove_cmd(self, interaction: discord.Interaction, user: discord.Member):
        t = await self._ctx(interaction)
        await self.remove_user(interaction.guild, t.id, user, interaction.user)
        await self._ack(interaction)

    @ticket.command(name="rename", description="Benennt den Ticket-Channel um")
    async def rename_cmd(self, interaction: discord.Interaction, name: app_commands.Range[str, 2, 90]):
        t = await self._ctx(interaction)
        await self.rename(interaction.guild, t.id, name)
        await self._ack(interaction)

    @ticket.command(name="transfer", description="Übergibt das Ticket an ein anderes Teammitglied")
    async def transfer_cmd(self, interaction: discord.Interaction, staff: discord.Member):
        t = await self._ctx(interaction)
        await self.transfer(interaction.guild, t.id, staff, interaction.user)
        await self._ack(interaction)

    @ticket.command(name="priority", description="Setzt die Priorität")
    @app_commands.choices(priority=[app_commands.Choice(name=f"{e} {k}", value=k) for k, e in PRIORITIES.items()])
    async def priority_cmd(self, interaction: discord.Interaction, priority: app_commands.Choice[str]):
        t = await self._ctx(interaction)
        await self.set_priority(interaction.guild, t.id, priority.value, interaction.user)
        await self._ack(interaction)

    @ticket.command(name="lock", description="Sperrt/entsperrt das Schreiben für den User")
    async def lock_cmd(self, interaction: discord.Interaction):
        t = await self._ctx(interaction)
        await self.set_locked(interaction.guild, t.id, not t.locked, interaction.user)
        await self._ack(interaction)

    @ticket.command(name="note", description="Interne Staff-Notiz (nur im Dashboard sichtbar)")
    async def note_cmd(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 2000]):
        t = await self._ctx(interaction)
        await self.add_note(t.id, interaction.user, text)
        await self._ack(interaction)

    @ticket.command(name="transcript", description="Erstellt ein Transcript dieses Tickets")
    async def transcript_cmd(self, interaction: discord.Interaction):
        t = await self.ticket_for(interaction.channel_id)
        await self.require_staff(interaction, t, allow_opener=True)
        await interaction.response.defer(ephemeral=True)
        doc, _c = await build_transcript(interaction.channel, t, interaction.guild)
        await interaction.followup.send(file=discord.File(io.BytesIO(doc.encode()), f"ticket-{t.number:04d}.html"), ephemeral=True)


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(TicketStartButton, TicketOpenButton, TicketSelect, TicketAction, PrioritySelect)
    await bot.add_cog(Tickets(bot))
