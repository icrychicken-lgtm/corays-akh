"""Bewerbungssystem: Panel → Position wählen → mehrseitiges Formular → Review (Discord & Dashboard) → automatische DM."""
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta

import discord
from discord.ext import commands
from sqlalchemy import select

from app.bot.ui import BaseView, handle_exception, reply
from app.core.embeds import theme
from app.core.errors import UserError
from app.core.events import bus
from app.core.guild_config import config
from app.core.i18n import fill, i18n
from app.core.records import audit, log_event
from app.core.timeutil import as_utc, ts
from app.db.base import SessionLocal, session_scope, utcnow
from app.db.models import Application
from app.services.notify import is_staff

log = logging.getLogger("nova.applications")
STATUS_ICON = {"open": "🟡", "in_review": "🔵", "accepted": "🟢", "rejected": "🔴"}


class ApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:app:start"):
    def __init__(self, label: str = "Bewerben"):
        super().__init__(discord.ui.Button(label=label, emoji="📋", style=discord.ButtonStyle.primary, custom_id="nova:app:start"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.client.get_cog("Applications").begin(interaction)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "apply")


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nova:app:(?P<id>\d+):(?P<action>accepted|rejected|in_review)"):
    STYLE = {"accepted": (discord.ButtonStyle.success, "✅"), "rejected": (discord.ButtonStyle.danger, "⛔"), "in_review": (discord.ButtonStyle.secondary, "🔎")}

    def __init__(self, app_id: int, action: str, label: str | None = None):
        style, emoji = self.STYLE[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"nova:app:{app_id}:{action}"))
        self.app_id, self.action = app_id, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str], /):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction):
        cog: Applications = interaction.client.get_cog("Applications")  # type: ignore[assignment]
        try:
            await cog.require_reviewer(interaction)
            if self.action == "in_review":
                await interaction.response.defer()
                await cog.decide(interaction.guild, self.app_id, "in_review", interaction.user, "")
            else:
                _ = await i18n.for_guild(interaction.guild_id)
                await interaction.response.send_modal(DecisionModal(cog, self.app_id, self.action, _))
        except Exception as exc:  # noqa: BLE001
            await handle_exception(interaction, exc, "app_review")


class DecisionModal(discord.ui.Modal):
    def __init__(self, cog: "Applications", app_id: int, status: str, _):
        super().__init__(title=_("apps.decision_title")[:45])
        self.cog, self.app_id, self.status = cog, app_id, status
        self.reason = discord.ui.TextInput(label=_("apps.decision_reason")[:45], required=False, max_length=1000, style=discord.TextStyle.paragraph)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.cog.decide(interaction.guild, self.app_id, self.status, interaction.user, self.reason.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "app_decide")


class FormModal(discord.ui.Modal):
    def __init__(self, cog: "Applications", questions: list[dict], offset: int, total_pages: int, page: int, title: str):
        super().__init__(title=f"{title} ({page}/{total_pages})"[:45], timeout=900)
        self.cog, self.offset = cog, offset
        self.inputs: list[discord.ui.TextInput] = []
        for q in questions:
            ti = discord.ui.TextInput(label=q["label"][:45] or "?", required=bool(q.get("required", True)),
                                      style=discord.TextStyle.paragraph if q.get("style") == "paragraph" else discord.TextStyle.short,
                                      placeholder=(q.get("placeholder") or None) and q["placeholder"][:100],
                                      max_length=1000 if q.get("style") == "paragraph" else 200)
            self.inputs.append(ti)
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.page_done(interaction, [(ti.label, ti.value) for ti in self.inputs])

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_exception(interaction, error, "app_form")


class ContinueView(BaseView):
    def __init__(self, cog: "Applications", owner_id: int, label: str):
        super().__init__(owner_id=owner_id, timeout=900)
        self.cog = cog
        self.cont.label = label

    @discord.ui.button(style=discord.ButtonStyle.primary, emoji="➡️")
    async def cont(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await self.cog.show_page(interaction)


class PositionSelect(discord.ui.Select):
    def __init__(self, cog: "Applications", positions: list[str], placeholder: str):
        super().__init__(placeholder=placeholder, options=[discord.SelectOption(label=p[:100], value=p[:100]) for p in positions[:25]])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        self.cog.drafts[(interaction.guild_id, interaction.user.id)] = {"position": self.values[0], "answers": [], "page": 0, "ts": time.monotonic()}
        await self.cog.show_page(interaction)


class Applications(commands.Cog):
    module = "applications"
    help_category = "tickets"

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.drafts: dict[tuple[int, int], dict] = {}

    async def send_panel(self, guild: discord.Guild) -> discord.Message:
        cfg = await config.get(guild.id, "applications")
        channel = guild.get_channel(cfg.id("panel_channel") or 0)
        if not isinstance(channel, discord.TextChannel):
            raise UserError("apps.no_panel_channel")
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = th.embed(cfg.get("panel_title"), cfg.get("panel_text"), icon=False)
        if cfg.get("positions"):
            e.add_field(name=_("apps.positions"), value="\n".join(f"• {p}" for p in cfg["positions"])[:1024], inline=False)
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)
        view = discord.ui.View(timeout=None)
        view.add_item(ApplyButton(_("apps.apply")))
        return await channel.send(embed=e, view=view)

    async def require_reviewer(self, interaction: discord.Interaction) -> None:
        cfg = await config.get(interaction.guild_id, "applications")
        if not await is_staff(interaction.user, cfg.ids("reviewer_roles")):  # type: ignore[arg-type]
            raise UserError("apps.reviewer_only")

    async def begin(self, interaction: discord.Interaction) -> None:
        guild, user = interaction.guild, interaction.user
        if not await config.enabled(guild.id, "applications"):
            raise UserError("errors.module_disabled")
        cfg = await config.get(guild.id, "applications")
        if not cfg.get("questions"):
            raise UserError("apps.no_questions")
        async with SessionLocal() as s:
            last = (await s.execute(select(Application).where(Application.guild_id == guild.id, Application.user_id == user.id)
                                    .order_by(Application.created_at.desc()).limit(1))).scalar_one_or_none()
        if last and last.status in ("open", "in_review"):
            raise UserError("apps.pending")
        days = int(cfg.get("cooldown_days") or 0)
        if last and days and utcnow() - as_utc(last.created_at) < timedelta(days=days):
            raise UserError("apps.cooldown", date=ts(as_utc(last.created_at) + timedelta(days=days), "R"))
        positions = cfg.get("positions") or []
        _ = await i18n.for_guild(guild.id)
        if len(positions) > 1:
            view = BaseView(owner_id=user.id, timeout=300)
            view.add_item(PositionSelect(self, positions, _("apps.choose_position")))
            await reply(interaction, (await theme(guild)).info(_("apps.choose_title"), _("apps.choose_desc")), view=view)
            return
        self.drafts[(guild.id, user.id)] = {"position": positions[0] if positions else "", "answers": [], "page": 0, "ts": time.monotonic()}
        await self.show_page(interaction)

    async def show_page(self, interaction: discord.Interaction) -> None:
        draft = self.drafts.get((interaction.guild_id, interaction.user.id))
        if not draft:
            raise UserError("apps.expired")
        cfg = await config.get(interaction.guild_id, "applications")
        questions = cfg.get("questions") or []
        pages = [questions[i:i + 5] for i in range(0, len(questions), 5)]
        page = draft["page"]
        _ = await i18n.for_guild(interaction.guild_id)
        await interaction.response.send_modal(FormModal(self, pages[page], page * 5, len(pages), page + 1,
                                                        _("apps.form_title", position=draft["position"] or "Team")))

    async def page_done(self, interaction: discord.Interaction, answers: list[tuple[str, str]]) -> None:
        key = (interaction.guild_id, interaction.user.id)
        draft = self.drafts.get(key)
        if not draft:
            raise UserError("apps.expired")
        draft["answers"].extend({"q": q, "a": a} for q, a in answers)
        draft["page"] += 1
        cfg = await config.get(interaction.guild_id, "applications")
        total_pages = (len(cfg.get("questions") or []) + 4) // 5
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        if draft["page"] < total_pages:
            await interaction.response.send_message(
                embed=th.info(_("apps.page_saved_title"), _("apps.page_saved", page=draft["page"], total=total_pages)),
                view=ContinueView(self, interaction.user.id, _("apps.continue")), ephemeral=True)
            return
        self.drafts.pop(key, None)
        await interaction.response.defer(ephemeral=True)
        app = await self.submit(interaction.guild, interaction.user, draft["position"], draft["answers"])
        await interaction.followup.send(embed=th.success(_("apps.submitted_title"), _("apps.submitted", id=app.id)), ephemeral=True)

    async def submit(self, guild: discord.Guild, user: discord.Member, position: str, answers: list[dict]) -> Application:
        async with session_scope() as s:
            app = Application(guild_id=guild.id, user_id=user.id, user_name=str(user), position=position, answers=answers, status="open")
            s.add(app)
            await s.flush()
        bus.publish(guild.id, "application", {"action": "new", "id": app.id, "user": str(user), "position": position})
        await log_event(guild.id, "application", "submit", user=user, content=position, details={"id": app.id})
        cfg = await config.get(guild.id, "applications")
        channel = guild.get_channel(cfg.id("review_channel") or 0)
        if isinstance(channel, discord.TextChannel):
            msg = await channel.send(embed=await self.review_embed(guild, app, user), view=await self.review_view(guild, app.id))
            async with session_scope() as s:
                row = await s.get(Application, app.id)
                row.review_message_id = msg.id
        return app

    async def review_embed(self, guild: discord.Guild, app: Application, user: discord.abc.User | None = None) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        kind = {"accepted": "success", "rejected": "error", "in_review": "info"}.get(app.status, "primary")
        e = th.embed(_("apps.review_title", id=app.id, position=app.position or "—"), kind=kind, user=user, icon=False)
        e.description = f"<@{app.user_id}> · `{app.user_id}`\n{STATUS_ICON.get(app.status, '')} **{_('apps.status.' + app.status)}**"
        for qa in app.answers[:20]:
            e.add_field(name=qa["q"][:256], value=(qa["a"] or "—")[:1024], inline=False)
        if app.reviewer_name:
            e.add_field(name=_("apps.reviewer"), value=f"{app.reviewer_name}" + (f"\n> {app.decision_reason[:900]}" if app.decision_reason else ""), inline=False)
        if user is not None:
            e.set_thumbnail(url=user.display_avatar.url)
        return e

    async def review_view(self, guild: discord.Guild, app_id: int, done: bool = False) -> discord.ui.View:
        _ = await i18n.for_guild(guild.id)
        view = discord.ui.View(timeout=None)
        if not done:
            view.add_item(ReviewButton(app_id, "accepted", _("apps.accept")))
            view.add_item(ReviewButton(app_id, "rejected", _("apps.reject")))
            view.add_item(ReviewButton(app_id, "in_review", _("apps.in_review")))
        return view

    async def decide(self, guild: discord.Guild, app_id: int, status: str, reviewer: discord.abc.User, reason: str = "") -> Application:
        if status not in ("open", "in_review", "accepted", "rejected"):
            raise UserError("errors.invalid_input", value=status)
        async with session_scope() as s:
            app = await s.get(Application, app_id)
            if app is None or app.guild_id != guild.id:
                raise UserError("apps.not_found")
            previous = app.status
            app.status = status
            app.reviewer_id = reviewer.id
            app.reviewer_name = str(reviewer)
            app.decision_reason = reason or app.decision_reason
            app.updated_at = utcnow()
        bus.publish(guild.id, "application", {"action": "update", "id": app.id, "status": status})
        await log_event(guild.id, "application", status, user=reviewer, target_id=app.user_id, content=reason, details={"id": app.id})
        await audit(guild.id, reviewer.id, str(reviewer), f"application.{status}", f"#{app.id} {app.user_name}", {"reason": reason}, source="bot")
        cfg = await config.get(guild.id, "applications")
        member = guild.get_member(app.user_id)
        if status in ("accepted", "rejected") and previous != status:
            if status == "accepted" and member and cfg.id("accept_role"):
                role = guild.get_role(cfg.id("accept_role"))
                if role and role < guild.me.top_role:
                    try:
                        await member.add_roles(role, reason=f"Bewerbung #{app.id} angenommen")
                    except discord.HTTPException:
                        pass
            user = member or await self.bot.fetch_user(app.user_id)
            _ = await i18n.for_guild(guild.id)
            th = await theme(guild)
            template = cfg.get("accept_message" if status == "accepted" else "reject_message") or ""
            e = (th.success if status == "accepted" else th.error)(_("apps.dm_title", server=guild.name),
                                                                   fill(template, position=app.position, server=guild.name, user=user.mention))
            if reason:
                e.add_field(name=_("apps.decision_reason"), value=reason[:1024])
            try:
                await user.send(embed=e)
            except discord.HTTPException:
                pass
        channel = guild.get_channel(cfg.id("review_channel") or 0)
        if app.review_message_id and isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(app.review_message_id)
                await msg.edit(embed=await self.review_embed(guild, app, member),
                               view=await self.review_view(guild, app.id, done=status in ("accepted", "rejected")))
            except discord.HTTPException:
                pass
        return app


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(ApplyButton, ReviewButton)
    await bot.add_cog(Applications(bot))
