"""Onboarding: /setup (Auto-Einrichtung mit einem Klick), /guide (verständliche Erklärungen), Begrüßung beim Server-Beitritt."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from app.bot.ui import BaseView, reply
from app.config import settings
from app.core.embeds import fmt_num, theme
from app.core.errors import UserError
from app.core.guild_config import config
from app.core.i18n import i18n
from app.core.records import audit
from app.db.base import SessionLocal, session_scope
from app.db.models import TicketCategory
from app.services.notify import is_admin
from app.services.progression import total_for_level

log = logging.getLogger("nova.onboarding")




class SetupView(BaseView):
    def __init__(self, cog: "Onboarding", owner_id: int, labels: tuple[str, str]):
        super().__init__(owner_id=owner_id, timeout=300)
        self.cog = cog
        self.go.label, self.cancel.label = labels

    @discord.ui.button(style=discord.ButtonStyle.success, emoji="⚡")
    async def go(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        lines = await self.cog.auto_setup(interaction.guild, interaction.user)
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        e = th.success(_("setup.done_title"), "\n".join(lines))
        e.add_field(name=_("setup.next_title"), value=_("setup.next_text", url=settings.dashboard_url), inline=False)
        await interaction.edit_original_response(embed=e, view=None)
        self.stop()

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.edit_message(view=None)
        self.stop()


class GuideSelect(discord.ui.Select):
    def __init__(self, cog: "Onboarding", labels: dict[str, str]):
        topics = [("start", "👋"), ("xp", "⭐"), ("coins", "💰"), ("gangs", "🏴"), ("streams", "📡"), ("tickets", "🎫")]
        super().__init__(placeholder=labels["placeholder"], options=[discord.SelectOption(label=labels[k], value=k, emoji=e) for k, e in topics])
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=await self.cog.guide_embed(interaction.guild, self.values[0]), view=self.view)


class Onboarding(commands.Cog):
    module = "core"
    help_category = "admin"

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Auto-Setup (auch vom Dashboard genutzt) ──
    async def auto_setup(self, guild: discord.Guild, actor: discord.abc.User) -> list[str]:
        _ = await i18n.for_guild(guild.id)
        me = guild.me
        if not (me.guild_permissions.manage_channels and me.guild_permissions.manage_roles):
            raise UserError("setup.no_perms")
        created: list[str] = []
        reason = f"Auto-Setup durch {actor}"

        async def role(name: str, color: int = 0, mentionable: bool = False) -> discord.Role:
            r = discord.utils.find(lambda x: x.name.lower() == name.lower(), guild.roles)
            if r is None:
                r = await guild.create_role(name=name, colour=discord.Colour(color), mentionable=mentionable, reason=reason)
                created.append(f"🎭 {r.mention}")
            return r

        async def category(name: str, private: bool = False) -> discord.CategoryChannel:
            c = discord.utils.find(lambda x: x.name.lower() == name.lower(), guild.categories)
            if c is None:
                ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False), me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)} if private else {}
                if private:
                    for r in guild.roles:
                        if not r.is_default() and not r.managed and (r.permissions.administrator or r.permissions.manage_guild or r.permissions.moderate_members):
                            ow[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                c = await guild.create_category(name, overwrites=ow, reason=reason)
            return c

        async def text(name: str, cat: discord.CategoryChannel, readonly: bool = False, topic: str = "") -> discord.TextChannel:
            ch = discord.utils.find(lambda x: x.name == name, guild.text_channels)
            if ch is None:
                ow = dict(cat.overwrites)
                if readonly:
                    base = ow.get(guild.default_role, discord.PermissionOverwrite())
                    base.send_messages = False
                    ow[guild.default_role] = base
                    ow[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)
                ch = await guild.create_text_channel(name, category=cat, overwrites=ow, topic=topic or None, reason=reason)
                created.append(f"#️⃣ {ch.mention}")
            return ch

        stream_ping = await role("🔔 Stream-Ping", 0xC8A45D, mentionable=True)
        tiers = [await role("Stammzuschauer", 0x9A8C73), await role("Day One", 0xB89B5E), await role("OG Viewer", 0xE0C27A)]

        info = await category("〢 COMMUNITY")
        welcome = await text("willkommen", info, readonly=True, topic="Willkommen auf dem Server!")
        alerts = await text("stream-alerts", info, readonly=True, topic="🔴 Live-Benachrichtigungen")
        clips = await text("clips", info, readonly=True, topic="🎬 Die besten Clips")
        levels = await text("level-ups", info, readonly=True, topic="⭐ Level-Ups & Achievements")
        ideas = await text("vorschläge", info, topic="💡 /suggest – deine Ideen")
        gangnews = await text("gang-news", info, readonly=True, topic="🏴 Gang-News")
        support = await category("〢 SUPPORT")
        tickets = await text("tickets", support, readonly=True, topic="🎫 Ticket öffnen")
        ticket_cat = await category("〢 TICKETS", private=True)
        staff = await category("〢 STAFF", private=True)
        modlog = await text("mod-log", staff)
        botlog = await text("bot-logs", staff)
        apps = await text("bewerbungen", staff)

        async def patch(module: str, values: dict, enable: bool = True) -> None:
            cfg = dict(await config.get(guild.id, module))
            cfg.update(values)
            await config.save(guild.id, module, settings=cfg, enabled=enable)

        await patch("welcome", {"channel": str(welcome.id)})
        await patch("levels", {"announce": "custom", "announce_channel": str(levels.id)})
        await patch("profiles", {"achievement_channel": str(levels.id)})
        await patch("streamer", {"default_channel": str(alerts.id), "default_role": str(stream_ping.id), "clips_channel": str(clips.id),
                                 "loyalty_roles": [{"checkins": 5, "role": str(tiers[0].id)}, {"checkins": 25, "role": str(tiers[1].id)},
                                                   {"checkins": 100, "role": str(tiers[2].id)}]})
        await patch("moderation", {"log_channel": str(modlog.id)})
        await patch("logging", {"default_channel": str(botlog.id)})
        await patch("tickets", {"panel_channel": str(tickets.id), "category": str(ticket_cat.id), "transcript_channel": str(botlog.id)})
        await patch("applications", {"review_channel": str(apps.id), "panel_channel": str(tickets.id)})
        await patch("suggestions", {"channel": str(ideas.id)})
        await patch("gangs", {"announce_channel": str(gangnews.id)})

        from app.bot.modules.tickets import ensure_default_categories
        await ensure_default_categories(guild.id)
        for cog_name in ("Tickets", "Suggestions"):
            cog = self.bot.get_cog(cog_name)
            try:
                await cog.send_panel(guild)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                log.info("Panel %s nicht gesendet: %s", cog_name, exc)
        await audit(guild.id, actor.id, str(actor), "setup.auto", f"{len(created)} neu", source="bot")
        summary = [_("setup.created", count=len(created))] + created[:20]
        summary.append(_("setup.configured"))
        return summary

    @app_commands.command(name="setup", description="Richtet den Bot automatisch ein – Channels, Rollen, Tickets, Stream-Alerts")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction):
        if not await is_admin(interaction.user):  # type: ignore[arg-type]
            raise UserError("errors.no_permission")
        _ = await i18n.for_guild(interaction.guild_id)
        th = await theme(interaction.guild)
        e = th.embed(_("setup.title"), _("setup.intro"), icon=False)
        e.add_field(name=_("setup.will_create"), value=_("setup.list"), inline=False)
        e.set_footer(text=_("setup.footer"))
        view = SetupView(self, interaction.user.id, (_("setup.start"), _("common.cancel")))
        await reply(interaction, e, view=view)

    # ── Guide ──
    async def guide_embed(self, guild: discord.Guild, topic: str) -> discord.Embed:
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        lv = await config.get(guild.id, "levels")
        eco = await config.get(guild.id, "economy")
        gangs = await config.get(guild.id, "gangs")
        cur = f"{eco.get('currency_emoji')} {eco.get('currency_name')}"
        vals = dict(xp_min=lv.get("xp_min"), xp_max=lv.get("xp_max"), cooldown=lv.get("cooldown"), voice_xp=lv.get("voice_xp"),
                    reaction_xp=lv.get("reaction_xp"), stream_xp=lv.get("stream_xp"), lvl5=fmt_num(total_for_level(5)),
                    lvl10=fmt_num(total_for_level(10)), lvl25=fmt_num(total_for_level(25)), lvl50=fmt_num(total_for_level(50)),
                    currency=cur, daily=eco.get("daily_amount"), weekly=eco.get("weekly_amount"), streak7=eco.get("streak_7_bonus"),
                    streak30=eco.get("streak_30_bonus"), msg_min=eco.get("message_min"), msg_max=eco.get("message_max"),
                    gang_cost=fmt_num(gangs.get("create_cost", 5000)), gang_max=gangs.get("max_members", 15), bot=self.bot.user.name if self.bot.user else "Bot")
        e = th.embed(_(f"guide.{topic}.title"), _(f"guide.{topic}.text", **vals), icon=False)
        e.set_footer(text=_("guide.footer"))
        return e

    @app_commands.command(name="guide", description="Einfach erklärt: XP, Coins, Gangs, Streams, Tickets")
    @app_commands.guild_only()
    async def guide(self, interaction: discord.Interaction):
        _ = await i18n.for_guild(interaction.guild_id)
        view = BaseView(owner_id=interaction.user.id, timeout=600)
        labels = {k: _(f"guide.{k}.title") for k in ("start", "xp", "coins", "gangs", "streams", "tickets")} | {"placeholder": _("guide.placeholder")}
        view.add_item(GuideSelect(self, labels))
        await reply(interaction, await self.guide_embed(interaction.guild, "start"), view=view)

    # ── Begrüßung beim Hinzufügen ──
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        _ = await i18n.for_guild(guild.id)
        th = await theme(guild)
        e = th.embed(_("setup.join_title", bot=self.bot.user.name if self.bot.user else "Bot"), _("setup.join_text", url=settings.dashboard_url), icon=False)
        if self.bot.user:
            e.set_thumbnail(url=self.bot.user.display_avatar.url)
        channel = guild.system_channel if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages else \
            next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
        try:
            if channel:
                await channel.send(embed=e)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))
