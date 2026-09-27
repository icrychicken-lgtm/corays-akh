"""Suggestions: /suggest oder Panel-Button → Embed mit Voting; Staff: Annehmen / Ablehnen / Überdenken (Status im Embed)."""
from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands

from app.bot.ui import handle_exception, reply
from app.core.embeds import progress_bar, theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import log_event
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Suggestion, SuggestionVote
from app.services.guilds import next_number
from app.services.notify import is_staff

STATUS = {"pending": ("🟡", "primary"), "accepted": ("🟢", "success"), "denied": ("🔴", "error"), "considered": ("🤔", "warning")}


class SuggestPanelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:sg:new"):
    def __init__(self, label: str = "Vorschlag einreichen"):
        super().__init__(discord.ui.Button(label=label, emoji="💡", style=discord.ButtonStyle.primary, custom_id="nova:sg:new"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.send_modal(SuggestModal(_("sg.modal_title"), _("sg.modal_label")))


class SuggestModal(discord.ui.Modal):
    def __init__(self, title: str, label: str):
        super().__init__(title=title[:45])
        self.text = discord.ui.TextInput(label=label[:45], style=discord.TextStyle.paragraph, min_length=10, max_length=2000)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Suggestions").create(interaction, self.text.value)  # type: ignore[union-attr]

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "suggest")


class SuggestionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:sg:(?P<id>\d+):(?P<action>up|down|accepted|denied|considered)"):
    STYLE = {"up": (discord.ButtonStyle.success, "👍"), "down": (discord.ButtonStyle.danger, "👎"),
             "accepted": (discord.ButtonStyle.secondary, "✅"), "denied": (discord.ButtonStyle.secondary, "⛔"),
             "considered": (discord.ButtonStyle.secondary, "🤔")}

    def __init__(self, sid: int, action: str, label: str | None = None, row: int | None = None):
        style, emoji = self.STYLE[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"nova:sg:{sid}:{action}", row=row))
        self.sid, self.action = sid, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog: Suggestions = interaction.client.get_cog("Suggestions")  # type: ignore[assignment]
        try:
            if self.action in ("up", "down"):
                await cog.vote(interaction, self.sid, 1 if self.action == "up" else -1)
            else:
                cfg = await config.get(interaction.guild_id, "suggestions")
                if not await is_staff(interaction.user, cfg.ids("staff_roles")):  # type: ignore[arg-type]
                    raise UserError("errors.no_permission")
                _ = await i18n.for_guild(interaction.guild_id)
                await interaction.response.send_modal(StaffReasonModal(cog, self.sid, self.action, _))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "suggestion_btn")


class StaffReasonModal(discord.ui.Modal):
    def __init__(self, cog: "Suggestions", sid: int, status: str, _):
        super().__init__(title=_("sg.status." + status)[:45])
        self.cog, self.sid, self.status = cog, sid, status
        self.reason = discord.ui.TextInput(label=_("sg.reason")[:45], required=False, max_length=1000, style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog.set_status(interaction.guild, self.sid, self.status, interaction.user, self.reason.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "suggestion_status")


@app_commands.guild_only()
class Suggestions(commands.Cog):
    module = "suggestions"
    help_category = "community"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def embed(self, guild: discord.Guild, s: Suggestion, author: discord.abc.User | None = None) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        cfg = await config.get(guild.id, "suggestions")
        th = await theme(guild)
        icon, kind = STATUS.get(s.status, ("•", "primary"))
        e = th.embed(_("sg.title", number=s.number), s.content[:4000], kind=kind, icon=False)
        if not cfg.get("anonymous") and author is not None:
            e.set_author(name=str(author), icon_url=author.display_avatar.url)
        total = s.upvotes + s.downvotes
        pct = (s.upvotes / total * 100) if total else 0
        e.add_field(name=_("sg.votes"), value=f"👍 **{s.upvotes}** · 👎 **{s.downvotes}**\n{progress_bar(s.upvotes, total or 1, 14)} {pct:.0f}%", inline=False)
        e.add_field(name=_("sg.status_label"), value=f"{icon} **{_('sg.status.' + s.status)}**", inline=True)
        if s.staff_name:
            e.add_field(name=_("sg.staff"), value=s.staff_name + (f"\n> {s.staff_reason[:900]}" if s.staff_reason else ""), inline=False)
        return e

    async def view(self, guild: discord.Guild, s: Suggestion) -> discord.ui.View:
        _ = await i18n.for_guild(guild.id)
        v = discord.ui.View(timeout=None)
        v.add_item(SuggestionButton(s.id, "up", str(s.upvotes)))
        v.add_item(SuggestionButton(s.id, "down", str(s.downvotes)))
        for st in ("accepted", "denied", "considered"):
            v.add_item(SuggestionButton(s.id, st, _("sg.btn." + st), row=1))
        return v

    async def send_panel(self, guild: discord.Guild) -> discord.Message:
        cfg = await config.get(guild.id, "suggestions")
        ch = guild.get_channel(cfg.id("channel") or 0)
        if not isinstance(ch, discord.TextChannel):
            raise UserError("sg.no_channel")
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        v = discord.ui.View(timeout=None)
        v.add_item(SuggestPanelButton(_("sg.submit")))
        return await ch.send(embed=th.embed(_("sg.panel_title"), cfg.get("panel_text"), icon=False), view=v)

    async def create(self, interaction: discord.Interaction, content: str) -> None:
        if not await config.enabled(interaction.guild_id, "suggestions"):
            raise UserError("errors.module_disabled")
        cfg = await config.get(interaction.guild_id, "suggestions")
        channel = interaction.guild.get_channel(cfg.id("channel") or 0)
        if not isinstance(channel, discord.TextChannel):
            raise UserError("sg.no_channel")
        async with session_scope() as db:
            number = await next_number(db, interaction.guild_id, "next_suggestion")
            s = Suggestion(guild_id=interaction.guild_id, number=number, user_id=interaction.user.id, user_name=str(interaction.user),
                           content=content, status="pending", channel_id=channel.id, upvotes=0, downvotes=0)
            db.add(s)
            await db.flush()
        msg = await channel.send(embed=await self.embed(interaction.guild, s, interaction.user), view=await self.view(interaction.guild, s))
        async with session_scope() as db:
            (await db.get(Suggestion, s.id)).message_id = msg.id
        if cfg.get("create_thread", True):
            try:
                await msg.create_thread(name=f"💡 #{number} – {content[:60]}", auto_archive_duration=10080)
            except discord.HTTPException:
                pass
        bus.publish(interaction.guild_id, "suggestion", {"action": "new", "id": s.id, "number": number})
        await log_event(interaction.guild_id, "suggestion", "new", user=interaction.user, content=content[:500], details={"number": number})
        _ = await i18n.for_guild(interaction.guild_id)
        await reply(interaction, (await theme(interaction.guild)).success(_("sg.created_title"), _("sg.created", link=msg.jump_url)))

    async def _refresh(self, guild: discord.Guild, s: Suggestion) -> None:
        ch = guild.get_channel(s.channel_id or 0)
        if isinstance(ch, discord.TextChannel) and s.message_id:
            author = guild.get_member(s.user_id)
            try:
                await ch.get_partial_message(s.message_id).edit(embed=await self.embed(guild, s, author), view=await self.view(guild, s))
            except discord.HTTPException:
                pass

    async def vote(self, interaction: discord.Interaction, sid: int, value: int) -> None:
        _ = await i18n.for_guild(interaction.guild_id)
        async with session_scope() as db:
            s = await db.get(Suggestion, sid)
            if s is None or s.guild_id != interaction.guild_id:
                raise UserError("sg.not_found")
            if s.status in ("accepted", "denied"):
                raise UserError("sg.closed")
            existing = await db.get(SuggestionVote, (sid, interaction.user.id))
            if existing and existing.vote == value:
                await db.delete(existing)
                msg = _("sg.vote_removed")
            elif existing:
                existing.vote = value
                msg = _("sg.vote_changed")
            else:
                db.add(SuggestionVote(suggestion_id=sid, user_id=interaction.user.id, vote=value))
                msg = _("sg.voted")
            await db.flush()
            from sqlalchemy import func, select
            up = (await db.execute(select(func.count()).where(SuggestionVote.suggestion_id == sid, SuggestionVote.vote == 1))).scalar_one()
            down = (await db.execute(select(func.count()).where(SuggestionVote.suggestion_id == sid, SuggestionVote.vote == -1))).scalar_one()
            s.upvotes, s.downvotes = up, down
        await interaction.response.edit_message(embed=await self.embed(interaction.guild, s, interaction.guild.get_member(s.user_id)),
                                                view=await self.view(interaction.guild, s))
        await interaction.followup.send(msg, ephemeral=True)
        bus.publish(interaction.guild_id, "suggestion", {"action": "vote", "id": sid, "up": up, "down": down})

    async def set_status(self, guild: discord.Guild, sid: int, status: str, staff: discord.abc.User, reason: str = "") -> Suggestion:
        if status not in STATUS:
            raise UserError("errors.invalid_input", value=status)
        async with session_scope() as db:
            s = await db.get(Suggestion, sid)
            if s is None or s.guild_id != guild.id:
                raise UserError("sg.not_found")
            s.status, s.staff_id, s.staff_name, s.staff_reason, s.updated_at = status, staff.id, str(staff), reason or None, utcnow()
        await self._refresh(guild, s)
        bus.publish(guild.id, "suggestion", {"action": "status", "id": sid, "status": status})
        await log_event(guild.id, "suggestion", status, user=staff, content=reason, details={"number": s.number})
        cfg = await config.get(guild.id, "suggestions")
        if cfg.get("dm_author", True):
            author = guild.get_member(s.user_id)
            if author:
                _ = await i18n.for_guild(guild.id)
                e = await self.embed(guild, s, author)
                e.title = _("sg.dm_title", number=s.number, server=guild.name)
                try:
                    await author.send(embed=e)
                except discord.HTTPException:
                    pass
        return s

    @app_commands.command(name="suggest", description="Reiche einen Vorschlag für den Server ein")
    async def suggest(self, interaction: discord.Interaction, idea: app_commands.Range[str, 10, 2000] | None = None):
        if idea:
            await interaction.response.defer(ephemeral=True)
            await self.create(interaction, idea)
        else:
            _ = await i18n.for_guild(interaction.guild_id)
            await interaction.response.send_modal(SuggestModal(_("sg.modal_title"), _("sg.modal_label")))


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(SuggestPanelButton, SuggestionButton)
    await bot.add_cog(Suggestions(bot))
